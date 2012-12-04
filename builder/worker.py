#!/usr/bin/env python
# vi:tabstop=4:expandtab

# Imports from the FUTURE
from __future__ import print_function

# STL Imports
import json
import logging
import os
import sys
import ConfigParser

# Third-Party Imports
import envoy
import redis
import pyres
from pyres.worker import Worker
from pyres.json_parser import dumps, loads

# Local Imports
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

logger.info('starting up build-api-{0} v{1}: {2}'.format(
            __name__, VERSION, config.get('general','name')))

class BuildWorker(object):
    '''
    '''


    @staticmethod
    def perform(build_id):
        '''
          The preform method is the workhorse of the async worker. It does the
          actual execution of the make script
        '''
        logger.debug("In perform for Job# %s",build_id)

        # make a connection to redis
        r = _redis(config)

        # load the job's payload
        json_blob = r.get("build-api:builds:by_id:{0}".format(build_id))
        logger.debug("Got JSON blob for payload:\n%s", json_blob)
        payload = loads(r.get("build-api:builds:by_id:{0}".format(build_id)))
        logger.debug("Loaded JSON payload for %s @ %s, from %s."%(
                                                payload['project'],
                                                payload['timestamp'],
                                                payload['queue'],
                                                )
                    )
        payload['status'] = "PROC"
        updatePayload(r,payload)
        logger.info('Updated the jobs status to PROC')


        os.chdir('{0}/{1}'.format(config.get('general', 'build_root'),payload['project']))
        e = envoy.run('./do_build')
        logger.info('envoy returned %s',e.status_code)
        logger.debug('envoy output:\n%s',e.std_out)
        payload['return_code'] = e.status_code
        payload['std_out'] = e.std_out
        payload['status'] = 'DONE'
        if e.status_code == 0:
            r.incr('build-api:stat:successes')
        else:
            r.incr('build-api:stat:failures')
        logger.info('Updated the jobs status to DONE')
        updatePayload(r,payload)


if __name__ == "__main__":

    # Set the logging level from command line
    numeric_level = getattr(logging, config.get("log","level").upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % loglevel)

    logging.basicConfig(level=numeric_level)

    #make a list of build queue subscriptions
    build_queues = []
    for chunk in config.get('general', 'build_queues').split(','):
        build_queues.append(chunk.strip())

    Worker.run( build_queues, server="{0}:{1}".format(
                                          config.get('redis','host'),
                                          config.get('redis','port'),
                                       )
              )
