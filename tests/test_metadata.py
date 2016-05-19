# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import unicode_literals
from __future__ import division
from pyLibrary.dot import set_default, wrap

from tests.base_test_class import ActiveDataBaseTest


class TestMetadata(ActiveDataBaseTest):


    def test_meta(self):
        test = wrap({
            "query": {"from": "meta.columns"},
            "data": [
                {"a": "b"}
            ]
        })

        settings = self.utils.fill_container(test, tjson=False)

        table_name = settings.index

        # WE REQUIRE A QUERY TO FORCE LOADING OF METADATA
        pre_test = {
            "query": {
                "from": table_name
            },
            "expecting_list": {
                "meta": {"format": "list"}, "data": [{"a": "b"}]
            }
        }
        self.utils.send_queries(pre_test)

        test = set_default(test, {
            "query": {
                "select": ["name", "table", "type", "nested_path"],
                "from": "meta.columns",
                "where": {"eq": {"table": table_name}}
            },
            "expecting_list": {
                "meta": {"format": "list"}, "data": [
                {"table": table_name, "name": "a", "type": "string", "nested_path": None}
            ]},
            "expecting_table": {
                "meta": {"format": "table"},
                "header": ["table", "name", "type", "nested_path"],
                "data": [[table_name, "a", "string", None]]
            },
            "expecting_cube": {
                "meta": {"format": "cube"},
                "edges": [
                    {
                        "name": "rownum",
                        "domain": {"type": "rownum", "min": 0, "max": 1, "interval": 1}
                    }
                ],
                "data": {
                    "table": [table_name],
                    "name": ["a"],
                    "type": ["string"],
                    "nested_type": [None]
                }
            }
        })
        self.utils.send_queries(test)

    def test_get_nested_columns(self):
        settings = self.utils.fill_container({
            "query": {"from": "meta.columns"},  # DUMMY QUERY
            "data": [
                {"o": 1, "_a": [
                    {"b": "x", "v": 2},
                    {"b": "y", "v": 3}
                ]},
                {"o": 2, "_a": {"b": "x", "v": 5}},
                {"o": 3, "_a": [
                    {"b": "x", "v": 7}
                ]},
                {"o": 4, "c": "x"}
            ]})

        table_name = settings.index

        # WE REQUIRE A QUERY TO FORCE LOADING OF METADATA
        pre_test = {
            "query": {
                "from": table_name,
                "sort": "o"
            },
            "expecting_list": {
                "meta": {"format": "list"}, "data": [
                    {"o": 1, "_a": [
                        {"b": "x", "v": 2},
                        {"b": "y", "v": 3}
                    ]},
                    {"o": 2, "_a": {"b": "x", "v": 5}},
                    {"o": 3, "_a": [{"b": "x", "v": 7}]},
                    {"o": 4, "c": "x"}
                ]}
        }
        self.utils.send_queries(pre_test)

        test = {
            "query": {
                "select": ["name", "table", "type", "nested_path"],
                "from": "meta.columns",
                "where": {"term": {"table": table_name}}
            },
            "expecting_list": {
                "meta": {"format": "list"},
                "data": [
                    {"table": table_name, "name": "_a", "type": "nested", "nested_path": "_a"},
                    {"table": table_name, "name": "_a.b", "type": "string", "nested_path": "_a"},
                    {"table": table_name, "name": "_a.v", "type": "double", "nested_path": "_a"},
                    {"table": table_name, "name": "c", "type": "string", "nested_path": None},
                    {"table": table_name, "name": "o", "type": "double", "nested_path": None},
                ]},
            "expecting_table": {
                "meta": {"format": "table"},
                "header": ["table", "name", "nested_path", "type"],
                "data": [
                    [table_name, "_a", "_a", "nested"],
                    [table_name, "_a.b", "_a", "string"],
                    [table_name, "_a.v", "_a", "double"],
                    [table_name, "c", None, "string"],
                    [table_name, "o", None, "double"]
                ]
            }
        }

        self.utils.send_queries(test)
