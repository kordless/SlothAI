import ast
import re
import math

from io import BytesIO

import requests
import json

import openai

from itertools import groupby

from google.cloud import vision, storage, documentai
from google.api_core.client_options import ClientOptions

from typing import Dict

import PyPDF2

from flask import current_app as app
from flask import url_for

from jinja2 import Environment

from enum import Enum

import datetime

# supress OpenAI resource warnings for unclosed sockets
import warnings
warnings.filterwarnings("ignore")

from SlothAI.web.custom_commands import random_word, random_sentence, chunk_with_page_filename
from SlothAI.web.models import User, Node, Template, Pipeline

from SlothAI.lib.tasks import Task, process_data_dict_for_insert, transform_data, get_values_by_json_paths, box_required, validate_dict_structure, TaskState, NonRetriableError, RetriableError, MissingInputFieldError, MissingOutputFieldError, UserNotFoundError, PipelineNotFoundError, NodeNotFoundError, TemplateNotFoundError
from SlothAI.lib.database import table_exists, add_column, create_table, get_columns, featurebase_query
from SlothAI.lib.util import fields_from_template, remove_fields_and_extras, strip_secure_fields, filter_document, load_from_storage, random_string

env = Environment()
env.globals['random_word'] = random_word
env.globals['random_sentence'] = random_sentence
env.globals['chunk_with_page_filename'] = chunk_with_page_filename

class DocumentValidator(Enum):
	INPUT_FIELDS = 'input_fields'
	OUTPUT_FIELDS = 'output_fields'

retriable_status_codes = [408, 409, 425, 429, 500, 503, 504]

processers = {}
processer = lambda f: processers.setdefault(f.__name__, f)

def process(task: Task) -> Task:
	user = User.get_by_uid(task.user_id)
	if not user:
		raise UserNotFoundError(task.user_id)
	
	pipeline = Pipeline.get(uid=task.user_id, pipe_id=task.pipe_id)
	if not pipeline:
		raise PipelineNotFoundError(pipeline_id=task.pipe_id)
	
	node_id = task.next_node()
	node = Node.get(uid=task.user_id, node_id=node_id)
	if not node:
		raise NodeNotFoundError(node_id=node_id)

	missing_field = validate_document(node, task, DocumentValidator.INPUT_FIELDS)
	if missing_field:
		raise MissingInputFieldError(missing_field, node.get('name'))

	# template the extras off the node
	extras = evaluate_extras(node, task)
	if extras:
		task.document.update(extras)

	# grab the available token for the node
	if "openai_token" in node.get('extras'):
		task.document['OPENAI_TOKEN'] = extras.get('openai_token')

	# get the user
	user = User.get_by_uid(uid=task.user_id)

	# if "x-api-key" in node.get('extras'):
	task.document['X-API-KEY'] = user.get('db_token')
	# if "database_id" in node.get('extras'):
	task.document['DATABASE_ID'] = user.get('dbid')

	# processer methods are responsible for adding errors to documents
	task = processers[node.get('processor')](node, task)

	# TODO, decide what to do with errors and maybe truncate pipeline
	if task.document.get('error'):
		return task

	if "OPENAI_TOKEN" in task.document.keys():
		task.document.pop('OPENAI_TOKEN', None)
	if "X-API-KEY" in task.document.keys():
		task.document.pop('X-API-KEY', None)
	if "DATABASE_ID" in task.document.keys():
		task.document.pop('DATABASE_ID', None)

	# strip out the sensitive extras
	clean_extras(extras, task)
	missing_field = validate_document(node, task, DocumentValidator.OUTPUT_FIELDS)
	if missing_field:
		raise MissingOutputFieldError(missing_field, node.get('name'))

	return task

@processer
def jinja2(node: Dict[str, any], task: Task) -> Task:
	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	output_fields = template.get('output_fields')
	input_fields = template.get('input_fields')

	template_text = remove_fields_and_extras(template.get('text'))

	try:
		if template_text:
			jinja_template = env.from_string(template_text)
			jinja = jinja_template.render(task.document)
	except Exception as e:
		raise NonRetriableError("jinja2 processor: unable to render jinja")
			
	try:
		jinja_json = json.loads(jinja)
		for k,v in jinja_json.items():
			task.document[k] = v
	except Exception as e:
		raise NonRetriableError("jinja2 processor: unable to load jinja output as JSON.")

	return task


@processer
def embedding(node: Dict[str, any], task: Task) -> Task:
	# Load OpenAI key if needed
	if "text-embedding-ada-002" in task.document.get('model'):
		openai.api_key = task.document.get('openai_token')

	# Output and input fields
	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	output_fields = template.get('output_fields')
	input_fields = template.get('input_fields')
	
	if not input_fields:
		raise NonRetriableError("embedding processor: input_fields required.")
	
	if not output_fields:
		raise NonRetriableError("embedding processor: output_fields required.")
	
	# Loop through each input field and produce the proper output for each <key>_embedding output field
	for input_field in input_fields:
		input_field_name = input_field.get('name')

		# Define the output field name for embeddings
		output_field = f"{input_field_name}_embedding"

		# Check if the output field is in output_fields
		if output_field not in [field['name'] for field in output_fields]:
			raise NonRetriableError(f"'{output_field}' is not in 'output_fields'.")

		# Get the input data chunks
		input_data = task.document.get(input_field_name)

		# Initialize a list to store the embeddings
		embeddings = []

		try:
			if "text-embedding-ada-002" in task.document.get('model'):
				batch_size = 10
				for i in range(0, len(input_data), batch_size):
					batch = input_data[i:i + batch_size]
					embedding_results = openai.Embedding.create(input=batch, model=task.document.get('model'))
					embeddings.extend([_object.get('embedding') for _object in embedding_results.get('data')])
			else:
				embedding_results = []

		except Exception as ex:
			print(input_data)
			# Making non-retriable for now; you can handle different error cases as needed
			raise NonRetriableError(f"Exception talking to OpenAI ada embedding: {ex}")

		# Add the embeddings to the output field
		task.document[output_field] = embeddings

	return task


# complete dictionaries
@processer
def aidict(node: Dict[str, any], task: Task) -> Task:
	# output and input fields
	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	input_fields = template.get('input_fields')
	output_fields = template.get('output_fields')

	# Check if each input field is present in 'task.document'
	for field in input_fields:
		field_name = field['name']
		if field_name not in task.document:
			raise NonRetriableError(f"Input field '{field_name}' is not present in the document.")

	# replace single strings with lists
	task.document = process_input_fields(task.document, input_fields)

	# Check if there are more than one input fields and grab the iterate_field
	if len(input_fields) > 1:
		iterate_field_name = task.document.get('iterate_field')

		if not iterate_field_name:
			raise NonRetriableError("More than one input field requires an 'iterate_field' value in extras.")

		if iterate_field_name != "False" and iterate_field_name not in [field['name'] for field in input_fields]:
			raise NonRetriableError(f"'{iterate_field_name}' must be present in 'input_fields' when there are more than one input fields, or you may use 'False' for no iteration.")
	else:
		iterate_field_name = input_fields[0]['name']
	

	if "gpt" in task.document.get('model'):
		openai.api_key = task.document.get('openai_token')

		errors = []
		if iterate_field_name != "False":
			iterator = task.document.get(iterate_field_name)
		else:
			iterator = ['False']

		# just loop over them
		for iterate_index, item in enumerate(iterator):
			# item is not used...but we set iterate_index for the template
			task.document['iterate_index'] = iterate_index

			template_text = remove_fields_and_extras(template.get('text'))

			if template_text:
				jinja_template = env.from_string(template_text)
				prompt = jinja_template.render(task.document)
			else:
				raise NonRetriableError("Couldn't find template text.")

			retries = 3
			# try a few times
			for _try in range(retries):
				completion = openai.ChatCompletion.create(
					model = task.document.get('model'),
					messages = [
						{"role": "system", "content": "You write python dictionaries for the user. You don't write code, use preambles, text markup, or any text other than the output requested, which is a python dictionary."},
						{"role": "user", "content": prompt}
					]
				)

				answer = completion.choices[0].message

				ai_dict_str = answer.get('content').replace("\n", "").replace("\t", "").lower()
				ai_dict_str = re.sub(r'\s+', ' ', ai_dict_str).strip()
				ai_dict_str = ai_dict_str.strip('ai_dict = ')

				try:
					ai_dict = eval(ai_dict_str)
					for field in output_fields:
						field_name = field['name']

						# Check if the field_name is present in ai_dict
						if field_name in ai_dict:
							# Ensure that the field exists in task.document as a list
							if field_name not in task.document:
								task.document[field_name] = []

							# Append the value(s) from ai_dict to the corresponding list in task.document
							value = ai_dict[field_name]
							task.document[field_name].append(value)
						else:
							errors.append(f"The aidict processor didn't return the fields expected in output_fields for index: {iterate_index}.")
					# break out
					break				
				except (ValueError, SyntaxError, NameError):
					app.logger.warn(f"The AI failed to build a dictionary. Try #{_try}.")
					errors.append(f"The aidict processor was unable to evaluate the response from the AI for index: {iterate_index}.")
			else:				
				raise NonRetriableError(f"Tried {retries} to get a dictionary from the AI, but failed.")

		task.document['aidict_errors'] = errors
		task.document.pop('iterate_index')

		return task

	else:
		raise NonRetriableError("The aidict processor expects a supported model.")


# generate images off a prompt
@processer
def aiimage(node: Dict[str, any], task: Task) -> Task:
	# Output and input fields
	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	output_fields = template.get('output_fields')
	input_fields = template.get('input_fields')

	# Ensure there is only one input field
	if len(input_fields) != 1:
		raise NonRetriableError("Only one input field is allowed in 'input_fields'.")

	input_field = input_fields[0].get('name')

	# Check that there is only one output field
	if len(output_fields) != 1:
		raise NonRetriableError("Only one output field is allowed in 'output_fields'.")

	output_field = output_fields[0].get('name')

	# Apply [:1000] to the input field as the prompt
	for prompt in task.document.get(input_field):
		prompt = prompt[:1000]
		break

	if not prompt:
		raise NonRetriableError("Input field is required and should contain the prompt.")

	num_images = task.document.get('num_images', 0)
	if not num_images:
		num_images = 1

	if "dall-e" in task.document.get('model'):
		openai.api_key = task.document.get('openai_token')

		try:
			response = openai.Image.create(
				prompt=prompt,
				n=int(num_images),
				size="1024x1024"
			)
			urls = [[]]

			# Loop over the 'data' list and extract the 'url' from each item
			for item in response['data']:
				if 'url' in item:
					urls[0].append(item['url'])

			task.document[output_field] = urls

		except Exception as ex:
			# non-retriable error for now but add retriable as needed
			raise NonRetriableError(f"aiimage processor: exception talking to OpenAI image create: {ex}")
	else:
		task.document[output_field] = []

	return task


@processer
def read_file(node: Dict[str, any], task: Task) -> Task:
	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	output_fields = template.get('output_fields')
	output_field = output_fields[0].get('name')

	user = User.get_by_uid(uid=task.user_id)
	uid = user.get('uid')
	filename = task.document.get('filename')
	mime_type = task.document.get('content_type')

	if mime_type == "application/pdf":
		# Get the document
		gcs = storage.Client()
		bucket = gcs.bucket(app.config['CLOUD_STORAGE_BUCKET'])
		blob = bucket.blob(f"{uid}/{filename}")
		image_content = blob.download_as_bytes()

		# Create a BytesIO object for the PDF content
		pdf_content_stream = BytesIO(image_content)
		pdf_reader = PyPDF2.PdfReader(pdf_content_stream)
		num_pages = len(pdf_reader.pages)

		# processor for document ai
		opts = ClientOptions(api_endpoint=f"us-documentai.googleapis.com")
		client = documentai.DocumentProcessorServiceClient(client_options=opts)
		parent = client.common_location_path(app.config['PROJECT_ID'], "us")
		processor_list = client.list_processors(parent=parent)
		for processor in processor_list:
			name = processor.name
			break # stupid google objects

		pdf_processor = client.get_processor(name=name)

		texts = []

		# build seperate pages and process each, adding text to texts
		for page_num in range(num_pages):
			pdf_writer = PyPDF2.PdfWriter()
			pdf_writer.add_page(pdf_reader.pages[page_num])
			page_stream = BytesIO()
			pdf_writer.write(page_stream)

			# Get the content of the current page as bytes
			page_content = page_stream.getvalue()

			# load data
			raw_document = documentai.RawDocument(content=page_content, mime_type="application/pdf")

			# make request
			request = documentai.ProcessRequest(name=pdf_processor.name, raw_document=raw_document)
			result = client.process_document(request=request)
			document = result.document

			# move to texts
			texts.append(document.text.replace("'","`").replace('"', '``').replace("\n"," ").replace("\r"," ").replace("\t"," "))

			# Close the page stream
			page_stream.close()

	else:
		raise NonRetriableError("read_file processor: only supports PDFs. Upload with type set to `application/pdf`.")

	# update the document
	task.document[output_field] = texts
	
	return task


@processer
def callback(node: Dict[str, any], task: Task) -> Task:
	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	
	user = User.get_by_uid(uid=task.user_id)
	if not user:
		raise UserNotFoundError(user_id=task.user_id)

	# need to rewrite to allow mapping tokens to the url template
	# alternately we could require a jinja template processor to be used in front of this to build the url
	auth_uri = task.document.get('callback_uri')

	# strip secure stuff out of the document
	document = strip_secure_fields(task.document) # returns document

	keys_to_keep = []
	if template.get('output_fields'):
		for field in template.get('output_fields'):
			for key, value in field.items():
				if key == 'name':
					keys_to_keep.append(value)

		if len(keys_to_keep) == 0:
			data = document
		else:
			data = filter_document(document, keys_to_keep)
	else:
		data = document

	# must add node_id and pipe_id
	data['node_id'] = node.get('node_id')
	data['pipe_id'] = task.pipe_id

	try:
		resp = requests.post(auth_uri, data=json.dumps(data))
		if resp.status_code != 200:
			message = f'got status code {resp.status_code} from callback'
			if resp.status_code in retriable_status_codes:
				raise RetriableError(message)
			else:
				raise NonRetriableError(message)
		
	except (
		requests.ConnectionError,
		requests.HTTPError,
		requests.Timeout,
		requests.TooManyRedirects,
		requests.ConnectTimeout,
	) as exception:
		raise RetriableError(exception)
	except Exception as exception:
		raise NonRetriableError(exception)

	return task


@processer
def split_task(node: Dict[str, any], task: Task) -> Task:
	template = Template.get(template_id=node.get('template_id'))
	input_fields = template.get('input_fields')
	output_fields = template.get('output_fields')
	if not input_fields:
		raise NonRetriableError("split_task processor: input fields required")
	if not output_fields:
		raise NonRetriableError("split_task processor: output fields required")

	inputs  = [n['name'] for n in input_fields]
	outputs = [n['name'] for n in output_fields] 

	batch_size = node.get('extras', {}).get('batch_size', None)

	# batch_size must be in extras
	if not batch_size:
		raise NonRetriableError("split_task processor: batch_size must be specified in extras!")
	
	try:
		batch_size = int(batch_size)
	except Exception as e:
		raise NonRetriableError(e)

	# all input / output fields should be lists of the same length to use split_task
	total_sizes = []
	for output in outputs:
		if output in inputs:
			field = task.document[output]
			if not isinstance(field, list):
				raise NonRetriableError(f"split_task processor: input fields must be list type: got {type(field)}")

			total_sizes.append(len(field))

		else:
			raise NonRetriableError(f"split_task processor: all output fields must be taken from input fields: output field {output} was not found in input fields.")

	if not all_equal(total_sizes):
		raise NonRetriableError("split_task processor: len of fields must be equal to re-batch a task")

	app.logger.info(f"Split Task: Task ID: {task.id}. Task Size: {total_sizes[0]}. Batch Size: {batch_size}. Number of Batches: {math.ceil(total_sizes[0] / batch_size)}")

	new_task_count = math.ceil(total_sizes[0] / batch_size)

	# split the data and re-task
	try:
		for i in range(new_task_count):
			batch_data = {}
			for field in outputs:
				batch_data[field] = task.document[field][:batch_size]
				del task.document[field][:batch_size]

			new_task = Task(
				id = random_string(),
				user_id=task.user_id,
				pipe_id=task.pipe_id,
				nodes=task.nodes[1:],
				document=batch_data,
				created_at=datetime.datetime.utcnow(),
				retries=0,
				error=None,
				state=TaskState.RUNNING,
			)
		
			new_task.create()
			app.logger.info(f"Split Task: spawning task {i} of projected {new_task_count}. It's ID is {new_task.id}")

	except Exception as e:
		app.logger.warn(f"Task with ID {task.id} was being split. An exception was raised during that process. {i - 1} tasks were created before that exception was raised.")
		raise NonRetriableError(e)

	# the initial task doesn't make it past split_task. so remove the rest of the nodes
	task.nodes = [task.next_node()]
	return task


@processer
def read_fb(node: Dict[str, any], task: Task) -> Task:
	user = User.get_by_uid(task.user_id)
	doc = {
		"dbid": user.get('dbid'),
		"db_token": user.get('db_token'),
		"sql": task.document['sql']
		}

	resp, err = featurebase_query(document=doc)
	if err:
		if "exception" in err:
			raise RetriableError(err)
		else:
			# good response from the server but query error
			raise NonRetriableError(err)

	# response data
	fields = []
	data = {}

	for field in resp.schema['fields']:
		fields.append(field['name'])
		data[field['name']] = []

	for tuple in resp.data:
		for i, value in enumerate(tuple):
			data[fields[i]].append(value)

	template = Template.get(template_id=node.get('template_id'))
	if not template:
		raise TemplateNotFoundError(template_id=node.get('template_id'))
	
	_keys = template.get('output_fields')
	if _keys:
		keys = [n['name'] for n in _keys]
		task.document.update(transform_data(keys, data))
	else:
		task.document.update(data)

	return task


from SlothAI.lib.schemar import Schemar
@processer
def write_fb(node: Dict[str, any], task: Task) -> Task:

	auth = {"dbid": task.document['DATABASE_ID'], "db_token": task.document['X-API-KEY']}

	template = Template.get(template_id=node.get('template_id'))
	_keys = template.get('input_fields') # must be input fields but not enforced
	keys = [n['name'] for n in _keys]
	data = get_values_by_json_paths(keys, task.document)

	table = task.document['table']
	tbl_exists, err = table_exists(table, auth)
	if err:
		raise RetriableError("issue checking for table in featurebase")
	
	# if it doesn't exists, create it
	if not tbl_exists:
		create_schema = Schemar(data=data).infer_create_table_schema() # check data.. must be lists
		err = create_table(table, create_schema, auth)
		if err:
			if "exception" in err:
				raise RetriableError(err)
			else:
				# good response from the server but query error
				raise NonRetriableError(err)
			
	# get columns from the table
	column_type_map, task.document['error'] = get_columns(table, auth)
	if task.document.get("error", None):
		raise Exception("unable to get columns from table in FeatureBase cloud")

	columns = [k for k in column_type_map.keys()]

	# add columns if data key cannot be found as an existing column
	for key in data.keys():
		if key not in columns:
			if not task.document.get("schema", None):
				task.document['schema'] = Schemar(data=data).infer_schema()

			err = add_column(table, {'name': key, 'type': task.document["schema"][key]}, auth)
			if err:
				if "exception" in err:
					raise RetriableError(err)
				else:
					# good response from the server but query error
					raise NonRetriableError(err)

			column_type_map[key] = task.document["schema"][key]

	columns, records = process_data_dict_for_insert(data, column_type_map, table)

	sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES {','.join(records)};"

	_, err = featurebase_query({"sql": sql, "dbid": task.document['DATABASE_ID'], "db_token": task.document['X-API-KEY']})
	if err:
		if "exception" in err:
			raise RetriableError(err)
		else:
			# good response from the server but query error
			raise NonRetriableError(err)
	
	return task

# remove these
# ============

@processer
def sloth_embedding(node: Dict[str, any], task: Task) -> Task:
	return sloth_processing(node, task, "embedding")

@processer
def sloth_keyterms(node: Dict[str, any], task: Task) -> Task:
	return sloth_processing(node, task, "keyterms")


def sloth_processing(node: Dict[str, any], task: Task, type) -> Task:
	if type == "embedding":
		path = "embed"
		response_key = "embeddings"
	elif type == "keyterms":
		path = "keyterms"
		response_key = "keyterms"
	else:
		raise Exception("sloth can only process embedding or keyterms.")

	template = Template.get(template_id=node.get('template_id'))
	input_fields, output_fields = fields_from_template(template.get('text'))

	if len(input_fields) != 1:
		task.document['error'] = "sloth only supports a single input field to get embedded at this time."

	if len(output_fields) != 1:
		task.document['error'] = "sloth only supports a single output field to hold the embedding at this time."

	data = {
		"text": task.document[input_fields[0]['name']]
		# data = get_values_by_json_paths(keys, task.document)
		# TODO: could be a nested key
	}

	defer, selected_box = box_required()
	if defer:
		task.document['error'] = "sloth virtual machine is being started"
		return task
	
	url = f"http://sloth:{app.config['SLOTH_TOKEN']}@{selected_box.get('ip_address')}:9898/{path}"

	try:
		# Send the POST request with the JSON data
		response = requests.post(url, data=json.dumps(data), headers={"Content-Type": "application/json"}, timeout=30)
	except Exception as ex:
		task.document['error'] = f"Exception raised connecting to sloth virtual machine: {ex}"
		return task

	# Check the response status code for success
	if response.status_code == 200:
		task.document[output_fields[0]['name']] = response.json().get(response_key)
	else:
		task.document['error'] = f"request to sloth_embedding virtual machine failed with status code {response.status_code}: {response.text}"

	return task


# helper functions
# ================
def process_input_fields(task_document, input_fields):
	updated_document = task_document
	
	for field in input_fields:
		field_name = field['name']
		if field_name in updated_document:
			# Check if the field is present in the document
			value = updated_document[field_name]
			if not isinstance(value, list):
				# If it's not already a list, replace it with a list containing the value
				updated_document[field_name] = [value]
	
	return updated_document


def validate_document(node, task: Task, validate: DocumentValidator):
	template = Template.get(template_id=node.get('template_id'))
	fields = template.get(validate)
	if fields:
		missing_key = validate_dict_structure(template.get('input_fields'), task.document)
		if missing_key:
			return missing_key
	
	return None


def evaluate_extras(node, task) -> Dict[str, any]:
	# get the node's current extras, which may be templated
	extras = node.get('extras', {})

	# combine with inputs
	combined_dict = extras.copy()
	combined_dict.update(task.document)

	# eval the extras from inputs_fields first
	extras_template = env.from_string(str(combined_dict))
	extras_from_template = extras_template.render(combined_dict)
	extras_eval = ast.literal_eval(extras_from_template)

	# remove the keys that were in the document
	extras_eval = {key: value for key, value in extras_eval.items() if key not in task.document}

	return extras_eval


def clean_extras(extras: Dict[str, any], task: Task):
	if extras:
		for k in extras.keys():
			if k in task.document.keys():
				del task.document[k]
	return task


# load template
def load_template(name="default"):
	# file path
	file_path = "./SlothAI/templates/prompts/%s.txt" % (name)

	try:
		with open(file_path, 'r', encoding='utf-8') as f:
			template = Template(f.read())
	except Exception as ex:
		print(ex)
		print("exception in loading template")
		template = None

	return template

def all_equal(iterable):
	g = groupby(iterable)
	return next(g, True) and not next(g, False)