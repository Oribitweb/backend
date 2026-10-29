import os
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Handler).serve_forever()