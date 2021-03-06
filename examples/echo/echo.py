#!/usr/bin/env python

from cocaine.server.worker import Worker
from cocaine.logging.defaults import log

__author__ = 'EvgenySafronov <division494@gmail.com>'


def echoV0(request, response):
    message = yield request.read()
    log.debug('Message received: \'{0}\'. Sending it back ...'.format(message))
    response.write(message)
    response.close()


def echoV1(request, response):
    response.write('Hi!')
    message = yield request.read()
    log.debug('Message received: \'{0}\'. Sending it back ...'.format(message))
    response.write(message)
    response.write('Another message.')
    message = yield request.read()
    log.debug('Message received: \'{0}\'. Sending it back ...'.format(message))
    response.write(message)
    response.close()


worker = Worker()
worker.run({
    'pingV0': echoV0,
    'pingV1': echoV1,
})
