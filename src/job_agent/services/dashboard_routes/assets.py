from __future__ import annotations
from http import HTTPStatus
from urllib.parse import urlsplit

from job_agent.services.dashboard_routes import route

@route("GET", r"/")
def serve_index(handler):
    handler._headers(HTTPStatus.OK, "text/html; charset=utf-8")
    handler.wfile.write(handler.template)

@route("GET", r"/favicon\.ico")
def serve_favicon(handler):
    handler._headers(HTTPStatus.NO_CONTENT, "image/x-icon")

@route("GET", r"/assets/dashboard\.css")
def serve_stylesheet(handler):
    handler._headers(HTTPStatus.OK, "text/css; charset=utf-8")
    handler.wfile.write(handler.stylesheet)

@route("GET", r"/assets/fonts/.*")
def serve_font(handler):
    path = urlsplit(handler.path).path
    font_asset = handler.font_assets.get(path)
    if font_asset is not None:
        handler._headers(HTTPStatus.OK, "font/woff2")
        handler.wfile.write(font_asset)
    else:
        handler.send_error(HTTPStatus.NOT_FOUND, "Font not found")

@route("GET", r"/js/.*\.js")
def serve_js(handler):
    path = urlsplit(handler.path).path
    asset = handler.js_assets.get(path)
    if asset is not None:
        handler._headers(HTTPStatus.OK, "application/javascript; charset=utf-8")
        handler.wfile.write(asset)
    else:
        handler.send_error(HTTPStatus.NOT_FOUND, "JS file not found")
