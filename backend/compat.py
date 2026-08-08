"""给 server 模块补上小工具，避免各个 feature 模块重复导入 Flask。"""

from flask import jsonify, request


def register_compat(server_module):
    server_module.jsonify_compat = jsonify

    def request_json():
        # 上游前端有几处 fetch 没带 Content-Type，所以用 force=True。
        return request.get_json(force=True, silent=True) or {}

    server_module.request_json = request_json
