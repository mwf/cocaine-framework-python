#!/usr/bin/env python
import StringIO

import qrcode

from cocaine.decorators import http
from cocaine.server.worker import Worker

__author__ = 'Evgeny Safronov <division494@gmail.com>'


"""
This example shows how to make simple HTTP Cocaine application using Cocaine Python Framework.

After waiting for http request, we read it and get some message from query string (?message=...). Then QR code
generation comes into.
Generated image is sending back via `response` stream.
"""


@http
def generate(request, response):
    request = yield request.read()
    try:
        message = request.request['message']
        out = StringIO.StringIO()
        img = qrcode.make(message)
        img.save(out, 'png')
        response.write_head(200, [('Content-type', 'image/png')])
        response.write(out.getvalue())
    except KeyError:
        response.write_head(400, [('Content-type', 'text/plain')])
        response.write('Query field "message" is required')
    except Exception as err:
        response.write_head(400, [('Content-type', 'text/plain')])
        response.write(str(err))
    finally:
        response.close()


w = Worker()
w.run({
    'generate': generate
})
