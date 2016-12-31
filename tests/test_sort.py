# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import division
from __future__ import unicode_literals

from pyDots import wrap
from tests import NULL
from tests.base_test_class import ActiveDataBaseTest, TEST_TABLE

lots_of_data = wrap([{"a": i} for i in range(30)])


class TestSorting(ActiveDataBaseTest):

    def test_name_and_direction_sort(self):
        test = {
            "data": [
                {"a": 1},
                {"a": 3},
                {"a": 4},
                {"a": 6},
                {"a": 2}
            ],
            "query": {
                "from": TEST_TABLE,
                "select": "a",
                "sort": {"a": "desc"}
            },
            "expecting_list": {
                "meta": {"format": "list"},
                "data": [6, 4, 3, 2, 1]
            }
        }
        self.utils.execute_es_tests(test)

    def test_groupby_and_sort(self):
        test = {
            "data": [
                {"a": "c", "value": 1},
                {"a": "c", "value": 3},
                {"a": "c", "value": 4},
                {"a": "c", "value": 6},
                {"a": "a", "value": 7},
                {"value": 99},
                {"a": "a", "value": 8},
                {"a": "a", "value": 9},
                {"a": "a", "value": 10},
                {"a": "a", "value": 11}
            ],
            "query": {
                "from": TEST_TABLE,
                "groupby": "a",
                "sort": "a"
            },
            "expecting_list": {
                "meta": {"format": "list"},
                "data": [
                    {"a": "a", "count": 5},
                    {"a": "c", "count": 4},
                    {"count": 1}
                ]
            },
            "expecting_table": {
                "meta": {"format": "table"},
                "header": ["a", "count"],
                "data": [
                    ["a", 5],
                    ["c", 4],
                    [NULL, 1]
                ]
            },
            "expecting_cube": {
                "meta": {"format": "cube"},
                "edges": [{"name": "a", "domain": {"type": "set", "partitions": [
                    {"value": "a"},
                    {"value": "c"}
                ]}}],
                "data": {
                    "count": [5, 4, 1]
                }
            }
        }
        self.utils.execute_es_tests(test)

    def test_groupby2_and_sort(self):
        test = {
            "data": [
                {"a": "c", "b": 1, "value": 1},
                {"a": "c", "b": 2, "value": 3},
                {"a": "c", "value": 4},
                {"a": "c", "b": 1, "value": 6},
                {"a": "a", "b": 1, "value": 7},
                {"value": 99},
                {"a": "a", "b": 1, "value": 8},
                {"a": "a", "b": 2, "value": 9},
                {"a": "a", "b": 2, "value": 10},
                {"a": "a", "value": 11}
            ],
            "query": {
                "from": TEST_TABLE,
                "groupby": ["a", "b"],
                "sort": [{"b": "desc"}, {"a": "asc"}]
            },
            "expecting_list": {
                "meta": {"format": "list"},
                "data": [
                    {"a": "a", "b": 2, "count": 2},
                    {"a": "c", "b": 2, "count": 1},
                    {"a": "a", "b": 1, "count": 1},
                    {"a": "c", "b": 1, "count": 2},
                    {"a": "a", "b": NULL, "count": 1},
                    {"a": "c", "b": NULL, "count": 1},
                    {"count": 1}
                ]
            },
            "expecting_table": {
                "meta": {"format": "table"},
                "header": ["a", "b", "count"],
                "data": [
                    ["a", 2, 2],
                    ["c", 2, 1],
                    ["a", 1, 1],
                    ["c", 1, 2],
                    ["a", NULL, 1],
                    ["c", NULL, 1],
                    [NULL, NULL, 1]
                ]
            },
            # "expecting_cube": {
            #     "meta": {"format": "cube"},
            #     "edges": [
            #         {"name": "a", "domain": {"type": "set", "partitions": [
            #             {"value": "a"},
            #             {"value": "c"}
            #         ]}},
            #         {"name": "b", "domain": {"type": "set", "partitions": [
            #             {"value": 2},
            #             {"value": 1}
            #         ]}}
            #
            #     ],
            #     "data": {
            #         "count": [[2, 1, 1], [1, 2, 1], [0, 0, 1]]
            #     }
            # }
        }
        self.utils.execute_es_tests(test)
