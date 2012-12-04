import redis
import pyres
from pyres.json_parser import dumps, loads

def commaSplice(string_in):
    build_queues = []
    for chunk in string_in.split(','):
        build_queues.append(chunk.strip())
    return build_queues

def _redis(config):
    r = redis.Redis(
                        host= config.get('redis', 'host'),
                        port= int(config.get('redis', 'port')),
                        db  = config.get('redis', 'db'),
                      )
    cleanupPyresErrors(r)
    return r

def _resq(config):
    redis_uri = "{0}:{1}".format( config.get('redis', 'host'),
                                  config.get('redis','port')
                                )
    return pyres.ResQ(server=redis_uri)

def updatePayload(r, payload):
    r.set(
            "build-api:builds:by_id:{0}".format(payload['id']),
            dumps(payload)
        )
    return True

def cleanupPyresErrors(r):
    """
    """
    #Cleanup pyres failures
    while (r.llen("resque:failed") > 0):
        failure = loads(r.lpop("resque:failed"))
        failed_id = failure['payload']['args'][0]
        payload = loads(r.get("build-api:builds:by_id:{0}".format(failed_id)))
        payload['error'] = failure
        payload['status'] = 'DONE'
        updatePayload(r,payload)
