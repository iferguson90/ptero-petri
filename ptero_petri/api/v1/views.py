from . import parsers
from ... import exceptions
from flask import g, request, url_for
from flask.ext.restful import Resource, marshal
import traceback


class NetListView(Resource):
    def post(self):
        net_key = g.backend.create_net(request.json)
        return {'net_key': net_key}, 201

class NetView(Resource):
    pass

class TokenListView(Resource):
    def post(self, net_key, place_name):
        try:
            color = g.backend.create_token(net_key, place_name)
        except Exception as e:
            traceback.print_exc()
            raise
        return {'color': color}, 201

class TokenView(Resource):
    pass
