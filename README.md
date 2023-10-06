# SlothAI: A Model Pipeline Manager
SlothAI provides a simple and ansycronous methodology to implement document-based pipelines (chains) for various machine learning models. It is designed to be fast as hell.

<img src="https://github.com/FeatureBaseDB/SlothAI/blob/SlothAI/SlothAI/static/sloth.png?raw=true" width="240"/>

SlothAI is implemented in Python to run on AppEngine containers, and takes advantage of Cloud Task queues. SlothAI uses queues to asynchronously run inferencing on documents.

Machine learning box deployment is managed using [Laminoid](https://github.com/FeatureBaseDB/Laminoid).

## But, Why?
SlothAI is similar to LangChain, AutoChain, Auto-GPT, Ray and other machine learning frameworks that provide software opinionated model chains and model method management. Unlike these other solutions, SlothAI addresses scalable asynchronous inferencing while making it easy to edit templates and manage pipeline flows in a simple UI.

SlothAI's strategy for simplicity and scale is based on opinionated storage and compute layers. SlothAI requires using a SQL engine that can run both binary set operations and vector similarity. It runs tasks on that data inside containers using task queues, or manages the calls to GPU boxes for running larger model inferencing.

## Pipeline Strategy
SlothAI creates *ingest pipelines* which contain *nodes*. Nodes may do anything, but they are essentially a function that transforms one field into one or more other fields. What happens between the field input and outputs can essentially be anything. Models are a typical workload run in nodes. Nodes, and thus the machine learning models they run, are run in sequence during ingestion. You can deseralize most calls to machine learning models using multiple calls to a pipeline's ingestion endpoint. Nodes may also be created that output any data in the document stream to FeatureBase.

The opinionated reason for using FeatureBase is due to its ability to process SQL to a) retrieve normal "tabular" data, b) run fast set operations (feature sets) using FB's binary tree storage layer, and c) run vector comparisons using FB's tuple storage layer. It is our belief that all three of these types of indexing will be required to run in union, to suffenciently and efficently serve machine learning models datasets. Datasets are, of course, invaluable in providing good prompt augmentation as well as managing training data for new models. 

Subsequent updates to this repo will implement other storage layers, such as PostgreSQL with pgvector support and b-store set operations. We are working on a PostgreSQL module that will be Open Source (we hope) which will help PostgreSQL do on a server some of what FeatureBase Cloud does as a service.

## Template Strategy
Templates are the heart of how we can effectively communicate with large language models. SlothAI provides some basic templates for talking to various models and allows you to create your own nodes and templates to call various models.

Templates are based on Jinja2, so there is some ability to do lightweight operations in the template as it is being sent to the model. Templates will also be generated by other nodes at some point.

### Sample Ingestion Graph
The following graph outlines an *ingestion pipeline* for new data that extracts keyterms, embeds the text and keyterms together, then forms a question about the text and keyterms using GPT-3.5-turbo:

<img src="https://raw.githubusercontent.com/FeatureBaseDB/SlothAI/SlothAI/SlothAI/static/pipeline_graph.png" width="360"/>

An alternate strategy would be to form the questions from a given text fragment from a larger document by first storing the vectors and keyterms in FB, then running a query pipeline on the resulting dataset to allow for similarity search across all ingested documents, instead of just the text fragment and keyterms.

## Sample Ingestion and Results
A sample ingestion pipeline with an instructor-xl embedding model & gpt-3.5-turbo model to embed and extract keyterms:

```
curl -X POST \
-H "Content-Type: application/json" \
-d '{"text":["There was a knock at the door, then silence."]}' \
"https://ai.featurebase.com/tables/L5IaljmIaox2H8r5U/ingest?token=9V1swMnuv0yonoywKqwtQ5_gD9"

# results can be returned with JSON or queried with SQL:
fbsql> SELECT _id, keyterms, text, embedding FROM test;

+---------+------------+-------------------+------------+
|   _id   |  keyterms  |       text        |  embedding |
+---------+------------+-------------------+------------+
| Oqhff1  |['knock','do| There was a knock | [0.02333,0.|
+---------+------------+-------------------+------------+
```

The instructor embedding model returns and stores a dense vector with a size of 768 elements:

```
-0.05440837,-0.016896732,-0.04767465,0.0016255669,0.0348847,0.0144764315,-0.0159672,-0.002682281,-0.04491195,0.025720688,0.044070743, etc.
```

This can be used to do similarity searches with SQL (sorted furtherest from bottom document):
```
fbsql> select questions, text, cosine_distance(select embedding from demo where _id=2, embedding) as distance from demo order by distance desc;

To CSV:
questions,text,distance
What kind of watch is mentioned in the document?,Mechanical Watch,0.26853132
What happened after there was a knock at the door?,"There was a knock at the door, then silence.",0.25531703
Who has died?,Stephen Hawking has died,0.24436438
What is the content of the document?,GPT-4,0.24306345
What is the document reflecting on?,"Reflecting on one very, very strange year at Uber",0.23889714
Who has died?,Bram Moolenaar has died,0.2298429
Who has passed away?,Steve Jobs has passed away.,0.22849554
What is the title of the message?,A Message to Our Customers,0.20595884
What did Replit do to the user's open-source project?,Replit used legal threats to kill my open-source project,0.17009544
What was the outcome of the fair use case involving Google copying the Java SE API?,Google’s copying of the Java SE API was fair use [pdf],0.16320163
What organization issued a DMCA takedown to YouTube-dl?,YouTube-dl has received a DMCA takedown from RIAA,0
```

## Development Notes
* Embeddings, keyterm extraction, and question forming nodes are supported.
* Create new custom nodes is supported through customized templates.
* Creation of ingestion for pipelines is implmented.
* Creation of nodes is implemented.
* Creation of templates is implmented.
* Versioning for templates can be done by the user via downloads.
* Vector balancing is being researched and developed. Templates will help with balancing.
* Support for new model deployment occurs in the Laminoid project and is a WIP.
* Storage layer for PostgreSQL/pgvector is in planning.
* Alternate auth methods are being considered.

## Authentication
Authentication is currently limited to FeatureBase tokens ONLY. You must have a [FeatureBase cloud](https://cloud.featurebase.com/) account to use the application.

Security to the Laminoid controller is done through box tokens assigned to network tags in Google Compute. This secures the deployment somewhat, but could be better.

It would be quite easy to add email authentication to the system, so this project could be run in a VPC-like setup.

## Configuration
Create a `config.py` configuration file in the root directory. Use `config.py.example` to populate.

Keys, tokens and IPs are created as needed.

### Dependencies - Conda
Install conda and activate a new environment:

```
conda create -n slothai python=3.9
conda activate slothai
```

Install the requirements:

```
pip3 install -r requirements
```

### Dependencies - FeatureBase
You will need a FeatureBase cloud account. It's free and requires your email address: https://cloud.featurebase.com/. We're poor, but we do have one friendly guy who may contact you. He's quite smart.

### Dependencies - Google Cloud
You'll need a whole bunch of Google Cloud things done. Enabling AppEngine, Compute, Cloud Tasks, domain names, firewalls and setting up credentials will eventually be documented here.

## Install

To deploy to your own AppEngine:

```
./scripts/deploy.sh --production
```

Deploy the cron.yaml after updating the key (use the `cron.yaml.example` file):

```
gcloud app deploy cron.yaml
```

Create an AppEngine task queue (from the name in `config.py`):

```
gcloud tasks queues create sloth-spittle --location=us-central1 
```

To deploy for local development:

```
./scripts/dev.sh
```

## Testing

To run tests run the following from the root directory:

```
pytest
```

To get test coverage run:
```
pytest --cov=SlothAI --cov-report=html
```

## Use
Login to the system using your custom domain name, or the *appspot* URL, which you can find in the Google Cloud console.

For local development, use the following URL:

```
http://localhost:8080
```

### Login
To login to the system, use your FeatureBase Cloud [database ID](https://cloud.featurebase.com/databases) and [API key](https://cloud.featurebase.com/configuration/api-keys) (token).
