import json

import attr
import torch
import networkx as nx

from tensor2struct.utils import registry, dataset
from third_party.spider import evaluation


@attr.s
class SpiderItem:
    text = attr.ib()
    code = attr.ib()
    schema = attr.ib()
    orig = attr.ib()
    orig_schema = attr.ib()


@attr.s
class Column:
    id = attr.ib()
    table = attr.ib()
    name = attr.ib()
    unsplit_name = attr.ib()
    orig_name = attr.ib()
    type = attr.ib()
    foreign_key_for = attr.ib(default=None)


@attr.s
class Table:
    id = attr.ib()
    name = attr.ib()
    unsplit_name = attr.ib()
    orig_name = attr.ib()
    columns = attr.ib(factory=list)
    primary_keys = attr.ib(factory=list)


@attr.s
class Schema:
    db_id = attr.ib()
    tables = attr.ib()
    columns = attr.ib()
    foreign_key_graph = attr.ib()
    orig = attr.ib()


def load_tables(paths):
    schemas = {}
    eval_foreign_key_maps = {}

    for path in paths:
        schema_dicts = json.load(open(path))
        for schema_dict in schema_dicts:
            tables = tuple(
                Table(id=i, name=name.split(), unsplit_name=name, orig_name=dataset.add_underscore(orig_name),)
                for i, (name, orig_name) in enumerate(
                    zip(schema_dict["table_names"], schema_dict["table_names_original"])
                )
            )
            columns = tuple(
                Column(
                    id=i,
                    table=tables[table_id] if table_id >= 0 else None,
                    name=col_name.split(),
                    unsplit_name=col_name,
                    orig_name=dataset.add_underscore(orig_col_name),
                    type=col_type,
                )
                for i, (
                    (table_id, col_name),
                    (_, orig_col_name),
                    col_type,
                ) in enumerate(
                    zip(
                        schema_dict["column_names"],
                        schema_dict["column_names_original"],
                        schema_dict["column_types"],
                    )
                )
            )

            # Link columns to tables
            for column in columns:
                if column.table:
                    column.table.columns.append(column)

            for column_id in schema_dict["primary_keys"]:
                # Register primary keys
                column = columns[column_id]
                column.table.primary_keys.append(column)

            foreign_key_graph = nx.DiGraph()
            for source_column_id, dest_column_id in schema_dict["foreign_keys"]:
                # Register foreign keys
                source_column = columns[source_column_id]
                dest_column = columns[dest_column_id]
                source_column.foreign_key_for = dest_column
                foreign_key_graph.add_edge(
                    source_column.table.id,
                    dest_column.table.id,
                    columns=(source_column_id, dest_column_id),
                )
                foreign_key_graph.add_edge(
                    dest_column.table.id,
                    source_column.table.id,
                    columns=(dest_column_id, source_column_id),
                )

            db_id = schema_dict["db_id"]
            assert db_id not in schemas
            schemas[db_id] = Schema(
                db_id, tables, columns, foreign_key_graph, schema_dict
            )
            eval_foreign_key_maps[db_id] = evaluation.build_foreign_key_map(schema_dict)

    return schemas, eval_foreign_key_maps


@registry.register("dataset", "spider")
class SpiderDataset(dataset.Dataset):
    def __init__(self, paths, tables_paths, db_path, limit=None):
        self.paths = paths
        self.db_path = db_path
        self.examples = []

        self.schemas, self.eval_foreign_key_maps = load_tables(tables_paths)

        for path in paths:
            raw_data = json.load(open(path))
            for entry in raw_data:
                item = SpiderItem(
                    text=entry["question_toks"],
                    code=entry["sql"],
                    schema=self.schemas[entry["db_id"]],
                    orig=entry,
                    orig_schema=self.schemas[entry["db_id"]].orig,
                )
                self.examples.append(item)

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]

    class Metrics:
        def __init__(self, dataset, etype):
            self.dataset = dataset
            self.etype = etype
            self.foreign_key_maps = {
                db_id: evaluation.build_foreign_key_map(schema.orig)
                for db_id, schema in self.dataset.schemas.items()
            }
            self.evaluator = evaluation.Evaluator(
                self.dataset.db_path, self.foreign_key_maps, etype,
            )
            self.results = []

        def add_one(self, item, inferred_code, orig_question=None):
            # switch from item.orig["query"] to item.orig["query_toks"] for vietnamese sql evaluation
            # ret_dict = self.evaluator.evaluate_one(
            #     item.schema.db_id, item.orig["query"], inferred_code
            # )
            ret_dict = self.evaluator.evaluate_one(
                item.schema.db_id, item.orig["query_toks"], inferred_code
            )

            if orig_question:
                ret_dict["orig_question"] = orig_question

            self.results.append(ret_dict)

        def add_beams(self, item, inferred_codes, orig_question=None):
            ret_dict = None
            # switch to name with underscore
            query_toks_w = [dataset.add_underscore(tok) for tok in item.orig["query_toks"]]
            for i, code in enumerate(inferred_codes):
                # if self.evaluator.isValidSQL(code, item.schema.db_id):
                #     ret_dict = self.evaluator.evaluate_one(
                #         item.schema.db_id, item.orig["query"], code
                #     )
                #     break
                if self.evaluator.isValidSQL(code, item.schema.db_id):
                    ret_dict = self.evaluator.evaluate_one(
                        item.schema.db_id, query_toks_w, code
                    )
                    break

            # if all failed
            if ret_dict is None:
                ret_dict = self.evaluator.evaluate_one(
                    item.schema.db_id, query_toks_w, inferred_codes[0]
                )

            if orig_question:
                ret_dict["orig_question"] = orig_question

            self.results.append(ret_dict)

        def finalize(self):
            self.evaluator.finalize()
            results = {"per_item": self.results, "total_scores": self.evaluator.scores}
            return results