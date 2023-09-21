import os
import sys
import json

import requests

import markdown
import markdown.extensions.fenced_code

from google.cloud import ndb

from flask import Blueprint, render_template, flash
from flask import make_response, Response
from flask import redirect, url_for, abort
from flask import request, send_file

import flask_login
from flask_login import current_user

from lib.util import random_string
from lib.ai import ai
from lib.tasks import list_tasks

from web.models import Table, Models

site = Blueprint('site', __name__)

import config

# client connection
client = ndb.Client()

@site.route('/sitemap.txt')
def sitemap():
	return render_template('pages/sitemap.txt')

@site.route('/tasks')
@site.route('/jobs')
@flask_login.login_required
def get_all_tasks():
	_tasks = list_tasks(current_user.uid)
	username = current_user.name
	return render_template(
		'pages/tasks.html', tasks=_tasks, username=username
	)

# main route
@site.route('/settings', methods=['GET'])
@flask_login.login_required
def settings():
	# get the user and their tables
	username = current_user.name
	api_token = current_user.api_token
	dbid = current_user.dbid

	return render_template(
		'pages/settings.html', username=username, api_token=api_token, dbid=dbid
	)

@site.route('/models', methods=['GET'])
@flask_login.login_required
def models():
	# get the user and their tables
	username = current_user.name
	api_token = current_user.api_token
	dbid = current_user.dbid
	models = Models.get_all()

	return render_template(
		'pages/models.html', username=username, dev=config.dev, api_token=api_token, dbid=dbid, models=models
	)

@site.route('/animate', methods=['GET'])
def serve_markdown():
	# Get the directory containing the script (one level above)
	script_directory = os.path.dirname(os.path.abspath(__file__))
	parent_directory = os.path.abspath(os.path.join(script_directory, '../static/'))

	# Define the relative path to your Markdown file
	relative_file_path = 'animate.mmd'


	readme_file = open(os.path.join(parent_directory, relative_file_path), "r")
	md_template_string = markdown.markdown(
		readme_file.read(), extensions=["fenced_code"]
	)

	return md_template_string


@site.route('/', methods=['GET'])
@site.route('/tables', methods=['GET'])
@flask_login.login_required
def tables():
	# get the user and their tables
	username = current_user.name

	tables = Table.get_all_by_uid(uid=current_user.uid)

	_tables = []
	with client.context():
		if tables:
			for table in tables:
				_tables.append(table)

	models = Models.get_all()

	return render_template('pages/tables.html', username=username, dev=config.dev, tables=_tables, models=models)

@site.route('/tables/<tid>', methods=['GET'])
@flask_login.login_required
def table_view(tid):
	# get the user and their tables
	username = current_user.name
	token = current_user.api_token

	# hack the _table (pipeline) up with the model info 
	_table = Table.get_by_uid_tid(uid=current_user.uid, tid=tid)

	if not _table:
		return redirect(url_for('site.tables'))
	
	if os.environ['GAE_VERSION'] == "staging":
		staging = True
		url = "https://staging-dot-" + config.project_id + ".appspot.com"
	else:
		staging = False
		url = ""
	
	return render_template('pages/table.html', username=username, dbid=current_user.dbid, token=token, dev=config.dev, table=_table, staging=staging, url=url)


