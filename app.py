from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello from minakata on Nomad!")

if __name__ == "__main__":
    print("Starting minakata server on port 8080...")
    HTTPServer(("", 8080), Handler).serve_forever()
