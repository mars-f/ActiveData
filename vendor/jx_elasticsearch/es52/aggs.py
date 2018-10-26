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

from operator import add

from jx_base.domains import SetDomain
from jx_base.expressions import TupleOp, NULL, TRUE
from jx_base.query import DEFAULT_LIMIT
from jx_elasticsearch import post as es_post
from jx_elasticsearch.es52.decoders import DefaultDecoder, AggsDecoder, ObjectDecoder, DimFieldListDecoder
from jx_elasticsearch.es52.expressions import split_expression_by_depth, AndOp, Variable, NullOp, split_expression_by_path
from jx_elasticsearch.es52.setop import get_pull_stats
from jx_elasticsearch.es52.util import aggregates
from jx_python import jx
from jx_python.expressions import jx_expression_to_function
from jx_python.jx import first
from mo_dots import listwrap, Data, wrap, literal_field, set_default, coalesce, Null, FlatList, unwrap, unwraplist, concat_field, relative_field, split_field, tail_field, startswith_field
from mo_future import text_type, sort_using_key
from mo_json import EXISTS, OBJECT, NESTED
from mo_json.typed_encoder import encode_property
from mo_logs import Log
from mo_logs.strings import quote, expand_template
from mo_math import Math, MAX, UNION
from mo_times.timer import Timer

DEBUG = False

COMPARE_TUPLE = """
(a, b)->{
    int i=0;
    for (dummy in a){  //ONLY THIS FOR LOOP IS ACCEPTED (ALL OTHER FORMS THROW NullPointerException)
        if (a[i]==null){
            if (b[i]==null){
                return 0; 
            }else{
                return -1*({{dir}});
            }//endif
        }else if (b[i]==null) return {{dir}};

        if (a[i]!=b[i]) {
            if (a[i] instanceof Boolean){
                if (b[i] instanceof Boolean){
                    int cmp = Boolean.compare(a[i], b[i]);
                    if (cmp != 0) return cmp;
                } else {
                    return -1;
                }//endif                    
            }else if (a[i] instanceof Number) {
                if (b[i] instanceof Boolean) {
                    return 1                
                } else if (b[i] instanceof Number) {
                    int cmp = Double.compare(a[i], b[i]);
                    if (cmp != 0) return cmp;
                } else {
                    return -1;
                }//endif
            }else {
                if (b[i] instanceof Boolean) {
                    return 1;
                } else if (b[i] instanceof Number) {
                    return 1;
                } else {
                    int cmp = ((String)a[i]).compareTo((String)b[i]);
                    if (cmp != 0) return cmp;
                }//endif
            }//endif
        }//endif
        i=i+1;
    }//for
    return 0;
}
"""


MAX_OF_TUPLE = """
(Object[])Arrays.asList(new Object[]{{{expr1}}, {{expr2}}}).stream().{{op}}("""+COMPARE_TUPLE+""").get()
"""


def is_aggsop(es, query):
    if query.edges or query.groupby or any(a != None and a != "none" for a in listwrap(query.select).aggregate):
        return True
    return False


def get_decoders_by_path(query):
    """
    RETURN MAP FROM QUERY PATH TO LIST OF DECODER ARRAYS

    :param query:
    :return:
    """
    schema = query.frum.schema
    output = Data()

    if query.edges:
        if query.sort and query.format != "cube":
            # REORDER EDGES/GROUPBY TO MATCH THE SORT
            query.edges = sort_edges(query, "edges")
    elif query.groupby:
        if query.sort and query.format != "cube":
            query.groupby = sort_edges(query, "groupby")

    for edge in wrap(coalesce(query.edges, query.groupby, [])):
        limit = coalesce(edge.domain.limit, query.limit, DEFAULT_LIMIT)
        if edge.value != None and not isinstance(edge.value, NullOp):
            edge = edge.copy()
            vars_ = edge.value.vars()
            for v in vars_:
                if not schema.leaves(v.var):
                    Log.error("{{var}} does not exist in schema", var=v)
        elif edge.range:
            vars_ = edge.range.min.vars() | edge.range.max.vars()
            for v in vars_:
                if not schema[v.var]:
                    Log.error("{{var}} does not exist in schema", var=v)
        elif edge.domain.dimension:
            vars_ = edge.domain.dimension.fields
            edge.domain.dimension = edge.domain.dimension.copy()
            edge.domain.dimension.fields = [schema[v].es_column for v in vars_]
        elif all(edge.domain.partitions.where):
            vars_ = set()
            for p in edge.domain.partitions:
                vars_ |= p.where.vars()

        try:
            vars_ |= edge.value.vars()
            depths = set(c.nested_path[0] for v in vars_ for c in schema.leaves(v.var))
            if not depths:
                Log.error(
                    "Do not know of column {{column}}",
                    column=unwraplist([v for v in vars_ if schema[v] == None])
                )
            if len(depths) > 1:
                Log.error("expression {{expr|quote}} spans tables, can not handle", expr=edge.value)
        except Exception as e:
            # USUALLY THE SCHEMA IS EMPTY, SO WE ASSUME THIS IS A SIMPLE QUERY
            depths = "."

        output[literal_field(first(depths))] += [AggsDecoder(edge, query, limit)]
    return output


def sort_edges(query, prop):
    ordered_edges = []
    remaining_edges = getattr(query, prop)
    for s in query.sort:
        for e in remaining_edges:
            if e.value == s.value:
                if isinstance(e.domain, SetDomain):
                    pass  # ALREADY SORTED?
                else:
                    e.domain.sort = s.sort
                ordered_edges.append(e)
                remaining_edges.remove(e)
                break
        else:
            Log.error("Can not sort by {{expr}}, can only sort by an existing edge expression", expr=s.value)

    ordered_edges.extend(remaining_edges)
    return ordered_edges


def es_aggsop(es, frum, query):
    query = query.copy()  # WE WILL MARK UP THIS QUERY
    schema = frum.schema
    query_path = schema.query_path[0]

    select = listwrap(query.select)

    es_query = wrap({"path": query_path, "query": {}})  # TRACK THE PATH AND THE COMPOSED QUERY
    def remap_nested(new_path):
        if new_path != es_query.path:
            if startswith_field(es_query.path, new_path):
                # DEEPER
                es_query.query = wrap({
                    "aggs": {"_nested": set_default(
                        {"nested": {"path": es_query.path}},
                        es_query.query
                    )}
                })
            else:
                # SHALLOWER
                es_query.query = wrap({
                    "aggs": {"_nested": set_default(
                        {"reverse_nested": {"path": es_query.path if es_query.path != "." else None}},
                        es_query.query
                    )}
                })
            es_query.path = new_path

    new_select = Data()  # MAP FROM canonical_name (USED FOR NAMES IN QUERY) TO SELECT MAPPING
    formula = []
    for s in select:
        if s.aggregate == "count" and isinstance(s.value, Variable) and s.value.var == ".":
            s.query_path = schema.query_path[0]
            if schema.query_path == ".":
                s.pull = jx_expression_to_function("doc_count")
            else:
                s.pull = jx_expression_to_function({"coalesce": ["_nested.doc_count", "doc_count", 0]})
        elif isinstance(s.value, Variable):
            s.query_path = schema.query_path[0]
            if s.aggregate == "count":
                new_select["count_"+literal_field(s.value.var)] += [s]
            else:
                new_select[literal_field(s.value.var)] += [s]
        elif s.aggregate:
            split_select = split_expression_by_path(s.value, schema)
            for si_key, si_value in split_select.items():
                if si_value:
                    if s.query_path:
                        Log.error("can not handle more than one depth per select")
                    s.query_path = si_key
            formula.append(s)

    for canonical_name, many in new_select.items():
        for s in many:
            if s.aggregate in ("value_count", "count"):
                columns = frum.schema.values(s.value.var, exclude_type=(OBJECT, NESTED))
            else:
                columns = frum.schema.values(s.value.var)

            if s.aggregate == "count":
                canonical_names = []
                for column in columns:
                    cn = literal_field(column.es_column + "_count")
                    if column.jx_type == EXISTS:
                        canonical_names.append(cn + ".doc_count")
                        es_query.query.aggs[cn].filter.range = {column.es_column: {"gt": 0}}
                    else:
                        canonical_names.append(cn + ".value")
                        es_query.query.aggs[cn].value_count.field = column.es_column
                if len(canonical_names) == 1:
                    s.pull = jx_expression_to_function(canonical_names[0])
                else:
                    s.pull = jx_expression_to_function({"add": canonical_names})
            elif s.aggregate == "median":
                if len(columns) > 1:
                    Log.error("Do not know how to count columns with more than one type (script probably)")
                # ES USES DIFFERENT METHOD FOR PERCENTILES
                key = literal_field(canonical_name + " percentile")

                es_query.query.aggs[key].percentiles.field = columns[0].es_column
                es_query.query.aggs[key].percentiles.percents += [50]
                s.pull = jx_expression_to_function(key + ".values.50\\.0")
            elif s.aggregate == "percentile":
                if len(columns) > 1:
                    Log.error("Do not know how to count columns with more than one type (script probably)")
                # ES USES DIFFERENT METHOD FOR PERCENTILES
                key = literal_field(canonical_name + " percentile")
                if isinstance(s.percentile, text_type) or s.percetile < 0 or 1 < s.percentile:
                    Log.error("Expecting percentile to be a float from 0.0 to 1.0")
                percent = Math.round(s.percentile * 100, decimal=6)

                es_query.query.aggs[key].percentiles.field = columns[0].es_column
                es_query.query.aggs[key].percentiles.percents += [percent]
                es_query.query.aggs[key].percentiles.tdigest.compression = 2
                s.pull = jx_expression_to_function(key + ".values." + literal_field(text_type(percent)))
            elif s.aggregate == "cardinality":
                canonical_names = []
                for column in columns:
                    cn = literal_field(column.es_column + "_cardinality")
                    canonical_names.append(cn)
                    es_query.query.aggs[cn].cardinality.field = column.es_column
                if len(columns) == 1:
                    s.pull = jx_expression_to_function(canonical_names[0] + ".value")
                else:
                    s.pull = jx_expression_to_function({"add": [cn + ".value" for cn in canonical_names], "default": 0})
            elif s.aggregate == "stats":
                if len(columns) > 1:
                    Log.error("Do not know how to count columns with more than one type (script probably)")
                # REGULAR STATS
                stats_name = literal_field(canonical_name)
                es_query.query.aggs[stats_name].extended_stats.field = columns[0].es_column

                # GET MEDIAN TOO!
                median_name = literal_field(canonical_name + "_percentile")
                es_query.query.aggs[median_name].percentiles.field = columns[0].es_column
                es_query.query.aggs[median_name].percentiles.percents += [50]

                s.pull = get_pull_stats(stats_name, median_name)
            elif s.aggregate == "union":
                pulls = []
                for column in columns:
                    script = {"scripted_metric": {
                        'init_script': 'params._agg.terms = new HashSet()',
                        'map_script': 'for (v in doc['+quote(column.es_column)+'].values) params._agg.terms.add(v);',
                        'combine_script': 'return params._agg.terms.toArray()',
                        'reduce_script': 'HashSet output = new HashSet(); for (a in params._aggs) { if (a!=null) for (v in a) {output.add(v)} } return output.toArray()',
                    }}
                    stats_name = encode_property(column.es_column)
                    if column.nested_path[0] == ".":
                        es_query.query.aggs[stats_name] = script
                        pulls.append(jx_expression_to_function(stats_name + ".value"))
                    else:
                        es_query.query.aggs[stats_name] = {
                            "nested": {"path": column.nested_path[0]},
                            "aggs": {"_nested": script}
                        }
                        pulls.append(jx_expression_to_function(stats_name + "._nested.value"))

                if len(pulls) == 0:
                    s.pull = NULL
                elif len(pulls) == 1:
                    s.pull = pulls[0]
                else:
                    s.pull = lambda row: UNION(p(row) for p in pulls)
            elif s.aggregate == "count_values":
                # RETURN MAP FROM VALUE TO THE NUMBER OF TIMES FOUND IN THE DOCUMENTS
                # NOT A NESTED DOC, RATHER A MULTIVALUE FIELD
                pulls = []
                for column in columns:
                    script = {"scripted_metric": {
                        'params': {"_agg": {}},
                        'init_script': 'params._agg.terms = new HashMap()',
                        'map_script': 'for (v in doc['+quote(column.es_column)+'].values) params._agg.terms.put(v, Optional.ofNullable(params._agg.terms.get(v)).orElse(0)+1);',
                        'combine_script': 'return params._agg.terms',
                        'reduce_script': '''
                            HashMap output = new HashMap(); 
                            for (agg in params._aggs) {
                                if (agg!=null){
                                    for (e in agg.entrySet()) {
                                        String key = String.valueOf(e.getKey());
                                        output.put(key, e.getValue() + Optional.ofNullable(output.get(key)).orElse(0));
                                    } 
                                }
                            } 
                            return output;
                        '''
                    }}
                    stats_name = encode_property(column.es_column)
                    if column.nested_path[0] == ".":
                        es_query.query.aggs[stats_name] = script
                        pulls.append(jx_expression_to_function(stats_name + ".value"))
                    else:
                        es_query.query.aggs[stats_name] = {
                            "nested": {"path": column.nested_path[0]},
                            "aggs": {"_nested": script}
                        }
                        pulls.append(jx_expression_to_function(stats_name + "._nested.value"))

                if len(pulls) == 0:
                    s.pull = NULL
                elif len(pulls) == 1:
                    s.pull = pulls[0]
                else:
                    s.pull = lambda row: add(p(row) for p in pulls)
            else:
                if not columns:
                    s.pull = jx_expression_to_function(NULL)
                else:
                    pulls = []
                    for c in columns:
                        if relative_field(c.nested_path[0], schema.query_path[0]) == '.':
                            # PULL VALUE OUT OF THE stats AGGREGATE
                            es_query.query.aggs[literal_field(canonical_name)].extended_stats.field = c.es_column
                            pulls.append({"coalesce": [concat_field(literal_field(canonical_name), aggregates[s.aggregate]), s.default]})
                        else:
                            nest_name = literal_field(concat_field(c.nested_path[0], canonical_name))
                            es_query.query.aggs[nest_name] = {
                                "nested": {"path": c.nested_path[0]},
                                "aggs": {canonical_name: {"extended_stats": {"field": c.es_column}}}
                            }
                            pulls.append({"coalesce": [
                                concat_field(concat_field(nest_name, literal_field(canonical_name)), aggregates[s.aggregate]),
                                s.default
                            ]})
                    if len(pulls) == 1:
                        s.pull = jx_expression_to_function(pulls[0])
                    else:
                        s.pull = jx_expression_to_function({"sum": pulls})
            # TODO: there can be many depths
            es_query.path = s.query_path

    for i, s in enumerate(formula):
        # value_by_path = split_expression_by_path(s.value, schema=schema)
        canonical_name = literal_field(s.name)

        if isinstance(s.value, TupleOp):
            if s.aggregate == "count":
                # TUPLES ALWAYS EXIST, SO COUNTING THEM IS EASY
                s.pull = "doc_count"
            elif s.aggregate in ('max', 'maximum', 'min', 'minimum'):
                if s.aggregate in ('max', 'maximum'):
                    dir = 1
                    op = "max"
                else:
                    dir = -1
                    op = 'min'

                nully = TupleOp("tuple", [NULL]*len(s.value.terms)).partial_eval().to_es_script(schema).expr
                selfy = s.value.partial_eval().to_es_script(schema).expr

                script = {"scripted_metric": {
                    'init_script': 'params._agg.best = ' + nully + ';',
                    'map_script': 'params._agg.best = ' + expand_template(MAX_OF_TUPLE, {"expr1": "params._agg.best", "expr2": selfy, "dir": dir, "op": op}) + ";",
                    'combine_script': 'return params._agg.best',
                    'reduce_script': 'return params._aggs.stream().'+op+'(' + expand_template(COMPARE_TUPLE, {"dir": dir, "op": op}) + ').get()',
                }}
                if schema.query_path[0] == ".":
                    es_query.query.aggs[canonical_name] = script
                    s.pull = jx_expression_to_function(literal_field(canonical_name) + ".value")
                else:
                    es_query.path = query_path
                    es_query.query.aggs[canonical_name] = {
                        "nested": {"path": schema.query_path[0]},
                        "aggs": {"_nested": script}
                    }
                    s.pull = jx_expression_to_function(literal_field(canonical_name) + "._nested.value")
            else:
               Log.error("{{agg}} is not a supported aggregate over a tuple", agg=s.aggregate)
        elif s.aggregate == "count":
            es_query.query.aggs[literal_field(canonical_name)].value_count.script = s.value.partial_eval().to_es_script(schema).script(schema)
            s.pull = jx_expression_to_function(literal_field(canonical_name) + ".value")
        elif s.aggregate == "median":
            # ES USES DIFFERENT METHOD FOR PERCENTILES THAN FOR STATS AND COUNT
            key = literal_field(canonical_name + " percentile")

            es_query.query.aggs[key].percentiles.script = s.value.to_es_script(schema).script(schema)
            es_query.query.aggs[key].percentiles.percents += [50]
            s.pull = jx_expression_to_function(key + ".values.50\\.0")
        elif s.aggregate == "percentile":
            # ES USES DIFFERENT METHOD FOR PERCENTILES THAN FOR STATS AND COUNT
            key = literal_field(canonical_name + " percentile")
            percent = Math.round(s.percentile * 100, decimal=6)

            es_query.query.aggs[key].percentiles.script = s.value.to_es_script(schema).script(schema)
            es_query.query.aggs[key].percentiles.percents += [percent]
            s.pull = jx_expression_to_function(key + ".values." + literal_field(text_type(percent)))
        elif s.aggregate == "cardinality":
            # ES USES DIFFERENT METHOD FOR CARDINALITY
            key = canonical_name + " cardinality"

            es_query.query.aggs[key].cardinality.script = s.value.to_es_script(schema).script(schema)
            s.pull = jx_expression_to_function(key + ".value")
        elif s.aggregate == "stats":
            # REGULAR STATS
            stats_name = literal_field(canonical_name)
            es_query.query.aggs[stats_name].extended_stats.script = s.value.to_es_script(schema).script(schema)

            # GET MEDIAN TOO!
            median_name = literal_field(canonical_name + " percentile")
            es_query.query.aggs[median_name].percentiles.script = s.value.to_es_script(schema).script(schema)
            es_query.query.aggs[median_name].percentiles.percents += [50]

            s.pull = get_pull_stats(stats_name, median_name)
        elif s.aggregate == "union":
            # USE TERMS AGGREGATE TO SIMULATE union
            stats_name = literal_field(canonical_name)
            es_query.query.aggs[stats_name].terms.script_field = s.value.to_es_script(schema).script(schema)
            s.pull = jx_expression_to_function(stats_name + ".buckets.key")
        else:
            # PULL VALUE OUT OF THE stats AGGREGATE
            s.pull = jx_expression_to_function(concat_field(canonical_name, aggregates[s.aggregate]))
            es_query.query.aggs[canonical_name].extended_stats.script = s.value.to_es_script(schema).script(schema)

        es_query.path = s.query_path

    split_decoders = get_decoders_by_path(query)
    split_wheres = split_expression_by_path(query.where, schema=frum.schema)


    start = 0
    decoders = []
    paths = list(reversed(sorted(split_wheres.keys() | split_decoders.keys())))
    for path in paths:
        literal_path = literal_field(path)
        decoder = split_decoders[literal_path]
        where = split_wheres[literal_path]

        if decoder or where:
            remap_nested(path)

        for d in decoder:
            decoders.append(d)
            es_query.query = d.append_query(es_query.path, es_query.query, start)
            start += d.num_columns

        if where:
            w = AndOp("and", where).partial_eval()
            if w is not TRUE:
                filter_ = w.to_esfilter(schema)
                es_query.query = Data(
                    aggs={"_filter": set_default({"filter": filter_}, es_query.query)}
                )

    remap_nested(".")  # ALL ES QUERIES START AT NO NESTING

    decoders = jx.reverse(decoders)
    es_query.query.size = 0

    with Timer("ES query time", silent=not DEBUG) as es_duration:
        result = es_post(es, es_query.query, query.limit)

    Log.note("{{result}}", result=result)
    try:
        format_time = Timer("formatting", silent=not DEBUG)
        with format_time:
            result.aggregations.doc_count = coalesce(result.aggregations.doc_count, result.hits.total)  # IT APPEARS THE OLD doc_count IS GONE

            formatter, groupby_formatter, aggop_formatter, mime_type = format_dispatch[query.format]
            if query.edges:
                output = formatter(decoders, result.aggregations, start, query, select)
            elif query.groupby:
                output = groupby_formatter(decoders, result.aggregations, start, query, select)
            else:
                output = aggop_formatter(decoders, result.aggregations, start, query, select)

        output.meta.timing.formatting = format_time.duration
        output.meta.timing.es_search = es_duration.duration
        output.meta.content_type = mime_type
        output.meta.es_query = es_query.query
        return output
    except Exception as e:
        if query.format not in format_dispatch:
            Log.error("Format {{format|quote}} not supported yet", format=query.format, cause=e)
        Log.error("Some problem", cause=e)


EMPTY = {}
EMPTY_LIST = []


def drill(agg):
    deeper = agg.get("_filter") or agg.get("_nested")
    while deeper:
        agg = deeper
        deeper = agg.get("_filter") or agg.get("_nested")
    return agg


def aggs_iterator(aggs, decoders, coord=True):
    """
    DIG INTO ES'S RECURSIVE aggs DATA-STRUCTURE:
    RETURN AN ITERATOR OVER THE EFFECTIVE ROWS OF THE RESULTS

    :param aggs: ES AGGREGATE OBJECT
    :param decoders:
    :param coord: TURN ON LOCAL COORDINATE LOOKUP
    """
    depth = max(d.start + d.num_columns for d in decoders)

    def _aggs_iterator(agg, d):
        agg = drill(agg)

        if d > 0:
            for k, v in agg.items():
                if k == "_match":
                    v = drill(v)
                    for i, b in enumerate(v.get("buckets", EMPTY_LIST)):
                        b["_index"] = i
                        for a, parts in _aggs_iterator(b, d - 1):
                            yield a, parts + (b,)
                elif k == "_other":
                    for b in v.get("buckets", EMPTY_LIST):
                        for a, parts in _aggs_iterator(b, d - 1):
                            yield a, parts + (Null,)
                elif k == "_missing":
                    b = drill(v)
                    for a, parts in _aggs_iterator(b, d - 1):
                        yield a, parts + (b,)
                elif k.startswith("_join_"):
                    v["key"] = int(k[6:])
                    for a, parts in _aggs_iterator(v, d - 1):
                        yield a, parts + (v,)
        else:
            for k, v in agg.items():
                if k == "_match":
                    v = drill(v)
                    for i, b in enumerate(v.get("buckets", EMPTY_LIST)):
                        b["_index"] = i
                        yield drill(b,), (b,)
                elif k == "_other":
                    for b in v.get("buckets", EMPTY_LIST):
                        yield b, (Null,)
                elif k == "_missing":
                    yield drill(v,), (v,)
                elif k.startswith("_join_"):
                    v["_index"] = int(k[6:])
                    yield drill(v), (v,)

    if coord:
        for a, parts in _aggs_iterator(unwrap(aggs), depth - 1):
            coord = tuple(d.get_index(parts) for d in decoders)
            if any(c is None for c in coord):
                continue
            yield parts, coord, a
    else:
        for a, parts in _aggs_iterator(unwrap(aggs), depth - 1):
            yield parts, None, a


def count_dim(aggs, decoders):
    if any(isinstance(d, (DefaultDecoder, DimFieldListDecoder, ObjectDecoder)) for d in decoders):
        # ENUMERATE THE DOMAINS, IF UNKNOWN AT QUERY TIME
        for row, coord, agg in aggs_iterator(aggs, decoders, coord=False):
            for d in decoders:
                d.count(row)
        for d in decoders:
            d.done_count()
    new_edges = wrap([d.edge for d in decoders])
    return new_edges


format_dispatch = {}
from jx_elasticsearch.es52.format import format_cube

_ = format_cube

