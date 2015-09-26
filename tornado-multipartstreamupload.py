#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import uuid

import tornado.web
from tornado.ioloop import IOLoop
from tornado.options import define, options

MAX_BODY_SIZE = 4 * 1024 ** 3
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "upload")

define("port", type = int, default = 8000, help = "run on the given port")
define('debug', type = bool, default = False, help = "run in debug mode with autoreload")

class MultiPartStream():
    BOUNDARY, HEADER, ARG_DATA, FILE_DATA, END = range(5)
    CRLF = b'\r\n'

    def __init__(self, boundary):
        self.state = self.BOUNDARY
        self.boundary = boundary
        self.rest_chunk = None
        self.data_name = None
        self.file_handler = None

class StreamedFile(tornado.util.ObjectDict):
    pass

@tornado.web.stream_request_body
class MultiPartStreamHandler(tornado.web.RequestHandler):
    def prepare(self):
        super(MultiPartStreamHandler, self).prepare()

        self.request.connection.set_max_body_size(MAX_BODY_SIZE)
        try:
            content_type = self.request.headers['Content-Type']
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("no multipart/form-data")
            fields = content_type.split(";")
            for field in fields:
                k, sep, v = field.strip().partition("=")
                if k == "boundary" and v:
                    boundary = tornado.escape.utf8(v)
                    if boundary.startswith(b'"') and boundary.endswith(b'"'):
                        boundary = boundary[1:-1]
                    boundary = b"--" + boundary

                    self.multipart_stream = MultiPartStream(boundary)
                    break
            else:
                raise ValueError("multipart boundary not found")
        except ValueError as e:
            self.send_error(400, reason="Invalid multipart/form-data: %s" % e.message)
        except KeyError as e:
            self.send_error(400, reason="%s header field is missing" % e.message)

    def data_received(self, chunk):
        try:
            if self.multipart_stream.rest_chunk:
                chunk = self.multipart_stream.rest_chunk + chunk
                self.multipart_stream.rest_chunk = None

            while True:
                # BOUNDARY: Check the boundary
                if self.multipart_stream.state == self.multipart_stream.BOUNDARY:
                    # When the last two bytes missing from the chunk
                    if len(chunk) < len(self.multipart_stream.boundary) + 2:
                        self.multipart_stream.rest_chunk = chunk
                        break

                    if chunk.startswith(self.multipart_stream.boundary + self.multipart_stream.CRLF):
                        chunk = chunk[len(self.multipart_stream.boundary) + 2:]
                        self.multipart_stream.state = self.multipart_stream.HEADER
                        continue

                    # Last boundary
                    if chunk.startswith(self.multipart_stream.boundary + b'--'):
                        self.multipart_stream.state = self.multipart_stream.END
                        continue

                    raise ValueError("no initial boundary found")

                # HEADER: Parse Header
                elif self.multipart_stream.state == self.multipart_stream.HEADER:
                    idx = chunk.find(self.multipart_stream.CRLF + self.multipart_stream.CRLF)
                    if idx == -1:
                        self.multipart_stream.rest_chunk = chunk
                        break
                    else:
                        headers = tornado.httputil.HTTPHeaders.parse(chunk[:idx].decode("utf-8"))
                        disp_header = headers.get("Content-Disposition", "")
                        disposition, disp_params = tornado.httputil._parse_header(disp_header)
                        if disposition != "form-data":
                            raise ValueError("missing headers")
                        if not disp_params.get("name"):
                            raise ValueError("name value is missing")

                        chunk = chunk[idx + 4:]
                        name = disp_params["name"]
                        if disp_params.get("filename"):
                            ctype = headers.get("Content-Type", "application/unknown")
                            filepath = os.path.join(self.application.upload_dir, str(uuid.uuid4()))
                            self.request.files.setdefault(name, []).append(StreamedFile(
                                filename=disp_params["filename"], filepath=filepath, content_type=ctype))
                            self.multipart_stream.file_handler = open(filepath, 'w')
                            self.multipart_stream.state = self.multipart_stream.FILE_DATA
                            continue
                        else:
                            self.multipart_stream.data_name = name
                            self.multipart_stream.state = self.multipart_stream.ARG_DATA
                            continue

                # ARG_DATA: Load the argument value
                elif self.multipart_stream.state == self.multipart_stream.ARG_DATA:
                    idx = chunk.find(self.multipart_stream.CRLF + self.multipart_stream.boundary)
                    if idx == -1:
                        #No boundary in this chunk, memory buffer for argument value
                        self.multipart_stream.rest_chunk = chunk
                        break

                    else:
                        self.request.arguments.setdefault(self.multipart_stream.data_name, []).append(chunk[:idx]) # without CRLF
                        chunk = chunk[idx + 2:]                                 # step after the CRLF
                        self.multipart_stream.state = self.multipart_stream.BOUNDARY
                        continue

                # FILE_DATA: Save file chunks to hard disk
                elif self.multipart_stream.state == self.multipart_stream.FILE_DATA:
                    idx = chunk.find(self.multipart_stream.CRLF + self.multipart_stream.boundary)
                    if idx == -1:
                        #No boundary in this chunk, but the begin of the boundary is possible (< CRLF + boundary)
                        self.multipart_stream.file_handler.write(chunk[:-1 * (len(self.multipart_stream.boundary) + 1)])
                        self.multipart_stream.rest_chunk = chunk[-1 * (len(self.multipart_stream.boundary) + 1):]
                        break
                    else:
                        self.multipart_stream.file_handler.write(chunk[:idx])   # write without CRLF
                        self.multipart_stream.file_handler.close()
                        chunk = chunk[idx + 2:]                                 # step after the CRLF
                        self.multipart_stream.state = self.multipart_stream.BOUNDARY
                        continue

                #END: After the last boundary nothing happened
                elif self.multipart_stream.state == self.multipart_stream.END:
                    break
        except ValueError as e:
            self.send_error(400, reason="Invalid multipart/form-data: %s" % e.message)
            self.multipart_stream.state = self.multipart_stream.END

    def post(self):
        self.write("success")

        print self.request.files
        print self.request.arguments


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.render("index.html",
            xsrf_token = self.xsrf_token
        )

        
class Application(tornado.web.Application):
    def __init__(self):
        handlers = [
            (r"/", IndexHandler),
            (r"/upload", MultiPartStreamHandler),
        ]
        
        settings = {
            "template_path": TEMPLATE_PATH,
            "debug": options.debug,
            "xsrf_cookies": True,
        }
        self.upload_dir = UPLOAD_DIR
        tornado.web.Application.__init__(self, handlers, **settings)


def main():
    if not os.path.exists(UPLOAD_DIR):
        os.mkdir(UPLOAD_DIR)

    tornado.options.parse_command_line()
    application = Application()
    application.listen(options.port)
    IOLoop.current().start()


if __name__=='__main__':
    main()