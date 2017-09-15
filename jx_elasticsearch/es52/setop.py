# encoding: utf-8
#
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http:# mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from collections import Mapping

from jx_base.domains import ALGEBRAIC
from jx_base.query import DEFAULT_LIMIT
from jx_elasticsearch import es52, es09
from jx_elasticsearch.es52.expressions import Variable, LeavesOp
from jx_elasticsearch.es52.util import jx_sort_to_es_sort
from jx_python.containers.cube import Cube
from jx_python.expressions import jx_expression_to_function
from mo_collections.matrix import Matrix
from mo_dots import coalesce, split_field, set_default, Data, unwraplist, literal_field, unwrap, wrap, concat_field, relative_field, join_field
from mo_dots import listwrap
from mo_dots.lists import FlatList
from mo_json.typed_encoder import decode_property, untyped, decode_path
from mo_logs import Log
from mo_math import AND
from mo_math import MAX
from mo_times.timer import Timer

format_dispatch = {}


def is_setop(es, query):
    if not any(map(es.cluster.version.startswith, ["1.4.", "1.5.", "1.6.", "1.7.", "5."])):
        return False

    select = listwrap(query.select)

    if not query.edges:
        isDeep = len(split_field(query.frum.name)) > 1  # LOOKING INTO NESTED WILL REQUIRE A SCRIPT
        simpleAgg = AND([s.aggregate in ("count", "none") for s in select])  # CONVERTING esfilter DEFINED PARTS WILL REQUIRE SCRIPT

        # NO EDGES IMPLIES SIMPLER QUERIES: EITHER A SET OPERATION, OR RETURN SINGLE AGGREGATE
        if simpleAgg or isDeep:
            return True
    else:
        isSmooth = AND((e.domain.type in ALGEBRAIC and e.domain.interval == "none") for e in query.edges)
        if isSmooth:
            return True

    return False


def es_setop(es, query):
    schema = query.frum.schema

    es_query, filters = es52.util.es_query_template(query.frum.name)
    set_default(filters[0], query.where.partial_eval().to_esfilter(schema))
    es_query.size = coalesce(query.limit, DEFAULT_LIMIT)
    es_query.stored_fields = FlatList()

    selects = wrap([s.copy() for s in listwrap(query.select)])
    new_select = FlatList()
    schema = query.frum.schema
    # columns = schema.columns
    # nested_columns = set(c.names["."] for c in columns if c.nested_path[0] != ".")

    es_query.sort = jx_sort_to_es_sort(query.sort, schema)

    put_index = 0
    for select in selects:
        # IF THERE IS A *, THEN INSERT THE EXTRA COLUMNS
        if isinstance(select.value, LeavesOp):
            term = select.value.term
            if isinstance(term, Variable):
                for c in schema.leaves(term.var):
                    es_query.stored_fields += [c.es_column]
                    path = split_field(concat_field(select.name, literal_field(relative_field(decode_path(c.names["."]), term.var))))
                    new_name = literal_field(path[0])
                    remainder = join_field(path[1:])
                    new_select.append({
                        "name": new_name,
                        "value": Variable(c.es_column, verify=False),
                        "put": {"name": new_name, "index": put_index, "child": remainder},
                        "pull": jx_expression_to_function(concat_field("fields", literal_field(c.es_column)))
                    })
                    put_index += 1
            else:
                Log.error("not supported")
        elif isinstance(select.value, Variable):
            if select.value.var == "_id":
                new_select.append({
                    "name": select.name,
                    "value": select.value,
                    "pull": jx_expression_to_function("_id"),
                    "put": {"name": select.name, "index": put_index, "child": "."}
                })
                put_index += 1
            else:
                s_column = select.value.var
                # LEAVES OF OBJECT
                cols = schema.leaves(s_column)
                nested_selects = {}
                if cols:
                    for c in cols:
                        if c.es_column == "_id" or c.es_column.endswith("$exists"):
                            continue
                        if len(c.nested_path) == 1:
                            jx_name = decode_path(c.names["."])
                            es_query.stored_fields += [c.es_column]
                            new_select.append({
                                "name": select.name,
                                "value": Variable(c.es_column, verify=False),
                                "put": {"name": select.name, "index": put_index, "child": relative_field(jx_name, s_column)}
                            })
                        else:
                            nested_path = c.nested_path[0]
                            if nested_path not in nested_selects:
                                where = nested_selects[nested_path] = Data()
                                es_query.query.bool.must += [where]
                                where.nested.path = nested_path
                                where.nested.query.match_all = {}
                                where.nested.inner_hits._source = False
                                where.nested.inner_hits.stored_fields += [c.es_column]

                                pull = accumulate_nested_doc(nested_path)
                                new_select.append({
                                    "name": select.name,
                                    "value": select.value,
                                    "put": {"name": select.name, "index": put_index, "child": "."},
                                    "pull": pull
                                })
                            else:
                                nested_selects[nested_path].nested.inner_hits.stored_fields+=[c.es_column]
                else:
                    new_select.append({
                        "name": select.name,
                        "value": Variable("$dummy", verify=False),
                        "put": {"name": select.name, "index": put_index, "child": "."}
                    })
                put_index += 1
        else:
            es_query.script_fields[literal_field(select.name)] = {"script": {
                "lang": "painless",
                "inline": select.value.partial_eval().to_painless(schema).script(schema)
            }}
            new_select.append({
                "name": select.name,
                "pull": jx_expression_to_function("fields." + literal_field(select.name)),
                "put": {"name": select.name, "index": put_index, "child": "."}
            })
            put_index += 1

    for n in new_select:
        if n.pull:
            continue
        elif isinstance(n.value, Variable):
            n.pull = jx_expression_to_function(concat_field("fields", literal_field(n.value.var)))
        else:
            Log.error("Do not know what to do")

    with Timer("call to ES") as call_timer:
        Log.note("{{data}}", data=es_query)
        data = es09.util.post(es, es_query, query.limit)

    T = data.hits.hits

    try:
        formatter, groupby_formatter, mime_type = format_dispatch[query.format]

        output = formatter(T, new_select, query)
        output.meta.timing.es = call_timer.duration
        output.meta.content_type = mime_type
        output.meta.es_query = es_query
        return output
    except Exception as e:
        Log.error("problem formatting", e)


def accumulate_nested_doc(nested_path):
    """
    :param nested_path: THE PATH USED TO EXTRACT THE NESTED RECORDS
    :return: THE DE_TYPED NESTED OBJECT ARRAY
    """
    name = literal_field(nested_path)
    def output(doc):
        acc = []
        for h in doc.inner_hits[name].hits.hits:
            i = h._nested.offset
            obj = Data()
            for f, v in h.fields.items():
                local_path = decode_path(relative_field(f, nested_path))
                obj[local_path] = unwraplist(v)
            # EXTEND THE LIST TO THE LENGTH WE REQUIRE
            for _ in range(len(acc), i+1):
                acc.append(None)
            acc[i] = obj
        return acc
    return output


def format_list(T, select, query=None):
    data = []
    if isinstance(query.select, list):
        for row in T:
            r = Data()
            for s in select:
                r[s.put.name][s.put.child] = unwraplist(s.pull(row))
            data.append(r if r else None)
    elif isinstance(query.select.value, LeavesOp):
        for row in T:
            r = Data()
            for s in select:
                r[s.put.name][s.put.child] = unwraplist(s.pull(row))
            data.append(r if r else None)
    else:
        for row in T:
            r = None
            for s in select:
                v = unwraplist(s.pull(row))
                if v is None:
                    continue
                if s.put.child == ".":
                    r = v
                else:
                    if r is None:
                        r = Data()
                    r[s.put.child] = v

            data.append(r)

    return Data(
        meta={"format": "list"},
        data=data
    )


def format_table(T, select, query=None):
    data = []
    num_columns = (MAX(select.put.index) + 1)
    for row in T:
        r = [None] * num_columns
        for s in select:
            value = unwraplist(s.pull(row))

            if value == None:
                continue

            index, child = s.put.index, s.put.child
            if child == ".":
                r[index] = value
            else:
                if r[index] is None:
                    r[index] = Data()
                r[index][child] = value

        data.append(r)

    header = [None] * num_columns

    if isinstance(query.select, Mapping) and not isinstance(query.select.value, LeavesOp):
        for s in select:
            header[s.put.index] = s.name
    else:
        for s in select:
            if header[s.put.index]:
                continue
            if s.name == ".":
                header[s.put.index] = "."
            else:
                header[s.put.index] = split_field(s.name)[0]

    return Data(
        meta={"format": "table"},
        header=header,
        data=data
    )


def format_cube(T, select, query=None):
    table = format_table(T, select, query)

    if len(table.data) == 0:
        return Cube(
            select,
            edges=[{"name": "rownum", "domain": {"type": "rownum", "min": 0, "max": 0, "interval": 1}}],
            data={h: Matrix(list=[]) for i, h in enumerate(table.header)}
        )

    cols = zip(*unwrap(table.data))
    return Cube(
        select,
        edges=[{"name": "rownum", "domain": {"type": "rownum", "min": 0, "max": len(table.data), "interval": 1}}],
        data={h: Matrix(list=cols[i]) for i, h in enumerate(table.header)}
    )


set_default(format_dispatch, {
    None: (format_cube, None, "application/json"),
    "cube": (format_cube, None, "application/json"),
    "table": (format_table, None, "application/json"),
    "list": (format_list, None, "application/json")
})