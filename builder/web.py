#!/usr/bin/env python
# vi:tabstop=4:expandtab

# Imports from the FUTURE
from __future__ import print_function

# STL Imports
import json
import logging
import os
import sys
import datetime
import ConfigParser

# Third-Party Imports
import flask
import redis
import pyres
from pyres.json_parser import dumps, loads

# Local Imports
import worker
from util import commaSplice, _redis, _resq, updatePayload

# Versioning
VERSION='1.0.0'

# Configure the application
CONFIGURATION_FILES=(
        "/etc/build-api/defaults.cfg",
        "/etc/build-api/site.cfg",
        os.environ["HOME"]+"/.build-api",
    )
config = ConfigParser.ConfigParser()
config.read(CONFIGURATION_FILES)

logger = logging.getLogger('build-api-{0}'.format(__name__))

logger.info('starting up build-api-%s v%s: %s', __name__, VERSION, config.get('general','name'))

# Fire up flask framework
app = flask.Flask(__name__)

@app.route("/", methods=['GET'])
def index():
    """
    /

    Main Index

    """
    TEMPLATE="""
<HTML>
<HEAD>
<TITLE>build-api: Index</TITLE>
<LINK REL="stylesheet" type="text/css" href="/style.css" />
</HEAD>
<BODY>
<H1>build-api: Index</H1>
<H2>Build Queues</H2>
<TABLE>
<TR><TH>timestamp<TH>project_name<TH>Job Status<TH>return_code<TH>actions
{% for q in queues %}
<TR><TH COLSPAN=5><A HREF=/builds_for_queue/{{q.name}}>view {{q.name}}</A>
{% for b in q.builds %}
<TR>
<TD>{{ b.timestamp }}
    <TD>{{b.project}}
    <TD CLASS={%
    if b.status == "WAIT"
        %}waiting>waiting{%
    elif b.status == "PROC"
        %}processing>processing{%
    elif b.status == "DONE"
        %}done>done{%
    else
        %}>b.status{%
    endif %}
    <TD CLASS={%
    if b.return_code == 0
        %}success>succeeded{%
    elif b.return_code > 0
        %}failure>failed ({{b.return_code}}){%
    elif b.error is defined
        %}failure>system error{%
    else
        %}>{%
    endif
    %}
    <TD>{% if b.status == "DONE" %}<A HREF=/log/{{b.id}}>view log</A>{% endif %}
    {% if b.return_code > 0 and b.requeued == False %}
        | <A HREF=/requeue/{{b.id}}>re-queue job</A>
    {% endif %}
{% endfor %}
{% endfor %}
</TABLE>

<H2>Projects</H2>
<UL>
{% for p in projects %}
  <LI>{{p.name}} <A HREF=/project_rem?project={{p.name}}><FONT COLOR=red>X</FONT></A>
  <UL>repos
  {% for r in p.repos %}
    <LI>{{r.name}} <A HREF=/project_repo_rem?project={{p.name}}&repo={{r.name}}><FONT COLOR=red>X</FONT></A>
    <UL>refs
    {% for f in r.refs %}
      <LI>{{f}} <A HREF="/project_repo_ref_rem?project={{p.name}}&repo={{r.name}}&ref={{f}}"><FONT COLOR=red>X</FONT></A>
    {% endfor %}
    </UL>
  {% endfor %}
  </UL>
{% endfor %}
</UL>

<FORM ACTION=/project_add_update METHOD=POST>
<H4>Add/Update Branch Trigger for Project:</H4>
name:<INPUT NAME=project TYPE=text />
an identifier for the project (usually the repo name, eg. build-api) <BR>
repo:<INPUT NAME=repo TYPE=text />
the github repo name including owner (eg. ph1l/build-api)<BR>
refs:<INPUT NAME=refs TYPE=text />
comma seperated list of refs to trigger builds for (eg. for building on branch master "refs/heads/master")<BR>
<INPUT NAME=submit TYPE=submit />
</FORM>

<H2>Workers ({{ num_workers }} online)</H2>
<TABLE>
<TR><TH>Name<TH>PID<TH>Queue Subscriptions
{% for w in workers %}
<TR><TD>{{w.name}}<TD>{{w.pid}}<TD>{% for wq in w.queues %} {{wq}} {% endfor %}
{% endfor %}
</TABLE>
<P>pyres has processed {{ num_processed }} jobs;
{{ num_successes }} have succeeded,
{{ num_failures }} have failed,
{{ num_system_failures }} have failed with a system error.</P>
<P CLASS=footer>build-api v{{ version }}</P>
</BODY>
</HTML>
"""
    r = _redis(config)

    # get information about currently configured projects
    projects = []
    for project in sorted(r.smembers("build-api:projects")):

        project_repos = sorted(r.smembers("build-api:project:{0}:repos".format(project)))
        repos = []
        for repo in project_repos:

            project_repo_refs = sorted(r.smembers("build-api:project:{0}:repo:{1}:refs".format(project,repo)))
            refs=[]
            for ref in project_repo_refs:
                refs.append(ref)
            repos.append({'name': repo, 'refs': refs})
        projects.append({'name': project, 'repos': repos})

    # get an overview of current workers
    workers = []
    for line in sorted(r.smembers('resque:workers')):
        try:
            (name, pid, queue_list) = line.split(':',2)
        except:
            continue
        queues = queue_list.split(",")
        workers.append({'name':name, 'pid':pid, 'queues':queues})

    # get an overview of the queues
    display_builds = 10
    available_queues = sorted(r.smembers('resque:queues'))
    queues = []
    for q in available_queues:
        build_ids =r.lrange("build-api:build_ids:by_queue:{0}".format(q),0,10)
        builds=[]
        for i in build_ids:
            builds.append(loads(r.get("build-api:builds:by_id:{0}".format(i))))
        queues.append({'name': q, 'builds': builds})
    return flask.render_template_string(TEMPLATE, version=VERSION,
            num_workers     = r.scard('resque:workers'),
            num_processed   = r.get('resque:stat:processed'),
            num_successes   = r.get('build-api:stat:successes'),
            num_failures    = r.get('build-api:stat:failures'),
            num_system_failures    = r.get('resque:stat:failed'),
            projects= projects,
            queues= queues,
            workers= workers
        )

@app.route('/project_add_update', methods=['POST'])
def project_add_update():
    assert flask.request.method == 'POST'
    r = _redis(config)
    r.sadd('build-api:projects',flask.request.form['project'])
    r.sadd(
            'build-api:project:{0}:repos'.format(flask.request.form['project']),
            flask.request.form['repo']
        )
    for ref in commaSplice(flask.request.form['refs']):
        r.sadd(
                'build-api:project:{0}:repo:{1}:refs'.format(
                        flask.request.form['project'],
                        flask.request.form['repo']
                    ),
                ref
            )
    return flask.redirect(flask.url_for('index'))

@app.route('/project_repo_ref_rem', methods=['GET'])
def project_repo_ref_rem():
    assert flask.request.method == 'GET'

    project = flask.request.args.get('project', '')
    repo = flask.request.args.get('repo', '')
    ref = flask.request.args.get('ref', '')

    r = _redis(config)
    ref_key = 'build-api:project:{0}:repo:{1}:refs'.format(project, repo)
    r.srem( ref_key, ref )

    if r.scard(ref_key) == 0:
        r.delete(ref_key)

    return flask.redirect(flask.url_for('index'))

@app.route('/project_repo_rem', methods=['GET'])
def project_repo_rem():
    assert flask.request.method == 'GET'
    project = flask.request.args.get('project', '')
    repo = flask.request.args.get('repo', '')

    r = _redis(config)
    ref_key = 'build-api:project:{0}:repo:{1}:refs'.format(project, repo)
    if r.exists(ref_key):
        return "Can't delete repo with children.\n"
    repo_key = "build-api:project:{0}:repos".format(project)
    r.srem( repo_key, repo )
    if r.scard( repo_key ) == 0:
        r.delete( repo_key )

    return flask.redirect(flask.url_for('index'))

@app.route('/project_rem', methods=['GET'])
def project_rem():
    assert flask.request.method == 'GET'
    project = flask.request.args.get('project', '')

    r = _redis(config)
    repo_key = "build-api:project:{0}:repos".format(project)
    if r.exists(repo_key):
        return "Can't delete project with children.\n"
    r.srem('build-api:projects',project)
    return flask.redirect(flask.url_for('index'))

@app.route('/build/<string:project>/<string:queues>', methods=['POST'])
def build(project = '', queues = ''):
    assert flask.request.method == 'POST'

    # Fetch the payload out of params provided by the HTTP POST
    try:
        git_post_data = loads(flask.request.form['payload'])
    except KeyError, err:
        logger.warning('Payload wasn\'t in the POST Params\n')
        return "No payload provided\n"

    r = _redis(config)
    resq = _resq(config)

    if not r.sismember('build-api:projects',project):
        logger.info('Ignoring build request for unknown project %s.', project)
        return "Unknown Project.\n"

    # TODO: check that this request fulfills conditions for triggering the
    # build.
    #
    # Possible Conditions:
    #   - A tag of a certain format
    #   - a tag of a certain format, gpg-signed by a user in a pre-defined
    #     keyring.
    #   - a combination of the above?
    #

    def checkTrigger(r, project, git_post_data):
        triggered_repo = "{0}/{1}".format(
                git_post_data['repository']['owner']['name'],
                git_post_data['repository']['name'],
            )
        triggered_ref = git_post_data['ref']

        repos = r.smembers('build-api:project:{0}:repos'.format(project))
        for repo in repos:
            if repo != triggered_repo:
                continue
            # Check for branch
            for ref in r.smembers('build-api:project:{0}:repo:{1}:refs'.format(project, repo)):
                if ref == triggered_ref:
                    return "Triggered by ref {1} in repo {0}".format(
                            triggered_repo,
                            triggered_ref
                        )
        return None

    triggered_by = checkTrigger(r,project,git_post_data)

    if triggered_by == None:
        logger.info('Trigger missed, build skipped for %s.', project)
        return "Trigger Missed.\n"

    for queue in commaSplice(queues):
        # assign this build an ID for tracking
        build_id = r.incr("build-api:build_id_next")

        build_payload = {
            'id'            : build_id,
            'timestamp'     : datetime.datetime.now(),
            'project'       : project,
            'queue'         : queue,
            'git_post'      : git_post_data,
            'triggered_by'  : triggered_by,
            'return_code'   : None,
            'std_out'       : None,
            'status'        : 'WAIT',
            'requeued'      : False,
            }

        # add 
        r.set(
                "build-api:builds:by_id:{0}".format(build_id),
                dumps(build_payload)
             )

        r.lpush("build-api:build_ids:by_project:{0}".format(project),build_id)
        r.lpush("build-api:build_ids:by_queue:{0}".format(queue),build_id)
        resq.enqueue_from_string("worker.BuildWorker", queue, build_id)
        logger.info('Build enqueued for %s/%s.', project,queue)
        logger.debug('payload:\n%s', build_payload )
    return "Succesfully Queued all jobs.\n"

@app.route('/requeue/<string:old_build_id>', methods=['GET'])
def requeue( old_build_id = ''):
    """
    /requeue/BUILD_ID

    re-queue the specified job
    """
    assert flask.request.method == 'GET'

    r = _redis(config)
    resq = _resq(config)

    # get the payload to re-queue
    payload = loads(r.get("build-api:builds:by_id:{0}".format(old_build_id)))

    # make a copy of the payload and update
    old_payload = loads(r.get("build-api:builds:by_id:{0}".format(old_build_id)))
    old_payload['requeued'] = True
    updatePayload(r,old_payload)

    #Update the old payload

    # assign the requeued build an ID for tracking
    build_id = r.incr("build-api:build_id_next")
    # update the payload to new status
    payload['id'] = build_id
    payload['timestamp'] = datetime.datetime.now()
    payload['return_code'] = None
    payload['std_out'] = None
    payload['status'] = 'WAIT'

    # re-enqueue the job
    r.set(
            "build-api:builds:by_id:{0}".format(build_id),
            dumps(payload)
         )

    r.lpush("build-api:build_ids:by_project:{0}".format(payload['project']),build_id)
    r.lpush("build-api:build_ids:by_queue:{0}".format(payload['queue']),build_id)
    resq.enqueue_specific(worker.BuildWorker, payload['queue'], build_id)
    logger.info('Build re-enqueued for %s/%s.', payload['project'],payload['queue'])
    logger.debug('payload:\n%s', payload )
    return flask.redirect(flask.url_for('index'))

@app.route('/builds_for_queue/<string:queue_name>', methods=['GET'])
def builds_for_queue( queue_name=''):
    """
    /builds_for_queue/QUEUENAME

    list recent builds

    """
    TEMPLATE = """
<HTML>
<HEAD>
<TITLE>build-api: Build Queue: {{queue_name}}</TITLE>
<LINK REL="stylesheet" type="text/css" href="/style.css" />
</HEAD>
<BODY>
<H1>build-api: Build Queue: {{queue_name}}</H1>
<TABLE>
<TR><TH>timestamp<TH>project_name<TH>Job Status<TH>return_code<TH>actions
{% for b in builds %}
<TR>
<TD>{{ b.timestamp }}
    <TD>{{b.project}}
    <TD CLASS={%
    if b.status == "WAIT"
        %}waiting>waiting{%
    elif b.status == "PROC"
        %}processing>processing{%
    elif b.status == "DONE"
        %}done>done{%
    else
        %}>b.status{%
    endif %}
    <TD CLASS={%
    if b.return_code == 0
        %}success>succeeded{%
    elif b.return_code > 0
        %}failure>failed ({{b.return_code}}){%
    else
        %}>{%
    endif
    %}
    <TD><A HREF=/log/{{b.id}}>view log</A>
{% endfor %}
</TABLE>
<P CLASS=footer>build-api v{{ version }}</P>
</BODY>
</HTML>
"""
    assert flask.request.method == 'GET'

    r = _redis(config)

    build_ids = r.lrange(
            "build-api:build_ids:by_queue:{0}".format(queue_name),
            0, 100
        )

    builds= []
    for i in build_ids:
        builds.append(loads(r.get("build-api:builds:by_id:{0}".format(i))))

    return flask.render_template_string( TEMPLATE,
                                            queue_name=queue_name,
                                            builds=builds,
                                            version=VERSION
                                        )

@app.route('/log/<string:build_id>', methods=['GET'])
def log(build_id = ''):
    """
    /log/<BUILD_ID>

    show detailed information about build

    """
    TEMPLATE = """
<HTML>
<HEAD>
<TITLE>build-api: Build #{{ b.id }} Log</TITLE>
<LINK REL="stylesheet" type="text/css" href="/style.css" />
</HEAD>
<BODY>
<H1>build-api: Build #{{ b.id }} Log</H1>
<H3>Triggered by {{ b.triggered_by }}</H3>
<TABLE>
<TR><TD>Project<TD>{{ b.project }}
<TR><TD>Queue<TD>{{ b.queue }}
<TR><TD>Timestamp<TD>{{ b.timestamp }}
<TR><TD>Job Status<TD CLASS={%
    if b.status == "WAIT"
        %}waiting>waiting{%
    elif b.status == "PROC"
        %}processing>processing{%
    elif b.status == "DONE"
        %}done>done{%
    else
        %}>b.status{%
    endif %}
<TR><TD>Build Status<TD CLASS={%
    if b.return_code == 0
        %}success>succeeded{%
    elif b.return_code > 0
        %}failure>failed ({{b.return_code}}){%
    elif b.error is defined
        %}failure>error{%
    else
        %}>{%
    endif
    %}
</TABLE>
{% if b.error is defined %}
<H3>Build Error:</H3>
<P>Worker {{b.error.worker}} failed at {{b.error.failed_at}}.</P>
<PRE>
{% for line in b.error.backtrace %}
{{line}}{% endfor %}
</PRE>
{% endif %}
<P>Build Output:</P>
<PRE>{{ b.std_out }}</PRE>
<P CLASS=footer>build-api v{{ version }}</P>
</BODY>
</HTML>
"""
    assert flask.request.method == 'GET'
    r = _redis(config)
    payload = loads(r.get("build-api:builds:by_id:{0}".format(build_id)))
    return flask.render_template_string(TEMPLATE,
                                            b=payload,
                                            version=VERSION
                                        )

@app.route("/style.css", methods=['GET'])
def style():
    """
    /style.css

    css style sheet

    """
    TEMPLATE = """
body {
    background-color:#444444;
    color: #F8F8F8;
    font-family: "Lucida Console", Lucida, monospace;
}
a:link{
    color: #AA99FF;
    text-decoration: none;
}
a:visited{
    color: #AA99FF;
    text-decoration: none;
}
a:hover{
    color: #EEDDFF;
    text-decoration: none;
}
a:active{
    color: #FFFFFF;
    text-decoration: none;
}
h1 {
    font-size: 2em;
    color:#999999;
}
h2 {
    font-size: 1.67em;
    color:#999999;
}
h3 {
    font-size: 1.33em;
    color:#999999;
}
h4 {
    font-size: 1.21em;
    color:#999999;
}
h5 {
    font-size: 1.11em;
    color:#999999;
}
h6 {
    font-size: 1.06em;
    color:#999999;
}
p.footer {
    font-size: .67em;
    color:#999999;
}
table {
    background-color:#666666;
    border-collapse:collapse;
}
td, th {
    padding: 3px;
}
table, th, td {
    border: 1px solid black;
}
tr.success, td.success {
    background-color:#008800;
}
tr.failure, td.failure {
    background-color:#880000;
}
td.waiting {
    background-color:#FF8040;
}
td.processing {
    background-color:#008800;
}
td.done {
    background-color:#808080;
}
"""
    return flask.render_template_string(TEMPLATE)

if __name__ == "__main__":

    # Set the logging level from command line
    numeric_level = getattr(logging, config.get("log","level").upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % loglevel)

    logging.basicConfig(level=numeric_level)

    # Enable debug mode if specified
    if config.get("flask","debug"):
        app.debug = True

    # Launch the webservice
    app.run(host=config.get("flask","host"),
            port=int(config.get("flask","port"))
        )
