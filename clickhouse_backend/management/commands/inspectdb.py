import re

from django.core.management.commands.inspectdb import Command as DCommand
from django.db import connections

from clickhouse_backend import compat, models
from clickhouse_backend.utils.encoding import ensure_str


class Command(DCommand):
    db_module = "clickhouse_backend"

    def handle_inspection(self, options):
        connection = connections[options["database"]]
        # 'table_name_filter' is a stealth option
        table_name_filter = options.get("table_name_filter")

        def table2model(table_name):
            return re.sub(r"[^a-zA-Z0-9]", "", table_name.title())

        with connection.cursor() as cursor:
            yield "# This is an auto-generated Django model module."
            yield "# You'll have to do the following manually to clean this up:"
            yield "#   * Rearrange models' order"
            yield "#   * Make sure each model has one field with primary_key=True"
            yield (
                "#   * Remove `managed = False` lines if you wish to allow "
                "Django to create, modify, and delete the table"
            )
            yield (
                "# Feel free to rename the models, but don't rename db_table values or "
                "field names."
            )
            yield "from %s import models" % self.db_module
            known_models = []
            # Determine types of tables and/or views to be introspected.
            types = {"t"}
            if options["include_views"]:
                types.add("v")
            table_info = connection.introspection.get_table_list(cursor)
            table_info = {info.name: info for info in table_info if info.type in types}

            for table_name in options["table"] or sorted(name for name in table_info):
                if table_name_filter is not None and callable(table_name_filter):
                    if not table_name_filter(table_name):
                        continue
                try:
                    try:
                        constraints = connection.introspection.get_constraints(
                            cursor, table_name
                        )
                    except NotImplementedError:
                        constraints = {}
                    table_description = connection.introspection.get_table_description(
                        cursor, table_name
                    )
                except Exception as e:
                    yield "# Unable to inspect table '%s'" % table_name
                    yield "# The error was: %s" % e
                    continue

                model_name = table2model(table_name)
                yield ""
                yield ""
                yield "class %s(models.ClickhouseModel):" % model_name
                known_models.append(model_name)
                used_column_names = []  # Holds column names used in the table so far
                column_to_field_name = {}  # Maps column names to names of model fields

                for row in table_description:
                    column_name = row.name

                    (
                        att_name,
                        extra_params,  # Holds Field parameters such as 'db_column'.
                        comment_notes,  # Holds Field notes, to be displayed in a Python comment.
                    ) = self.normalize_col_name(column_name, used_column_names, False)

                    used_column_names.append(att_name)
                    column_to_field_name[column_name] = att_name

                    # Add comment.
                    if (
                        compat.dj_ge42
                        and connection.features.supports_comments
                        and row.comment
                    ):
                        extra_params["db_comment"] = row.comment

                    if extra_params:
                        param = ", ".join(
                            "%s=%r" % (k, v) for k, v in extra_params.items()
                        )
                    else:
                        param = ""

                    field_define = "".join(
                        self.inspect_field_type(row.type_code, param)
                    )
                    field_desc = f"{att_name} = {field_define}"
                    if comment_notes:
                        field_desc += "  # " + " ".join(comment_notes)
                    yield "    %s" % field_desc
                comment = None
                if info := table_info.get(table_name):
                    is_view = info.type == "v"
                    if connection.features.supports_comments:
                        comment = info.comment
                else:
                    is_view = False
                yield from self.get_meta(
                    table_name,
                    constraints,
                    column_to_field_name,
                    is_view,
                    False,
                    comment,
                )

    def inspect_field_type(self, column_type, param=""):
        column_type = ensure_str(column_type)

        if column_type.startswith("LowCardinality"):  # LowCardinality(Int16)
            param = self.merge_params(param, "low_cardinality=True")
            remain = yield from self.inspect_field_type(column_type[15:], param)
            return remain[1:]
        elif column_type.startswith("Nullable"):  # Nullable(Int16)
            param = self.merge_params(param, "null=True", "blank=True")
            remain = yield from self.inspect_field_type(
                column_type[9:], param
            )  # Nullable(Int16)
            return remain[1:]
        elif column_type.startswith("FixedString"):  # FixedString(20)
            i = 12
            while column_type[i].isdigit():
                i += 1
            param = self.merge_params(param, f"max_bytes={column_type[12:i]}")
            yield f"models.FixedStringField({param})"
            return column_type[i + 1 :]
        elif column_type.startswith(
            "DateTime64"
        ):  # DateTime64(6, 'UTC') or DateTime64(9)
            if int(column_type[11]) != models.DateTime64Field.DEFAULT_PRECISION:
                param = self.merge_params(param, f"precision={column_type[11]}")
            yield f"models.DateTime64Field({param})"

            if column_type[12] == ",":
                i = 15
                while column_type[i] != "'":
                    i += 1
                return column_type[i + 2 :]
            return column_type[13:]
        elif column_type.startswith("DateTime"):  # DateTime('UTC') or DateTime
            yield f"models.DateTimeField({param})"
            if len(column_type) > 8 and column_type[8] == "(":
                i = 10
                while column_type[i] != "'":
                    i += 1
                    return column_type[i + 2 :]
            return column_type[8:]
        elif column_type.startswith("Decimal"):  # Decimal(9, 3)
            i = 8
            while column_type[i].isdigit():
                i += 1
            max_digits = f"max_digits={column_type[8:i]}"
            i += 2
            j = i
            while column_type[i].isdigit():
                i += 1
            decimal_places = f"decimal_places={column_type[j:i]}"
            param = self.merge_params(param, max_digits, decimal_places)
            yield f"models.DecimalField({param})"
            return column_type[i + 1 :]
        elif column_type.startswith("Enum"):  # Enum8('a' = 1, 'b' = 2)
            i = 4
            while column_type[i].isdigit():
                i += 1
            typ = column_type[:i]
            choices = []
            name, value, remain = self.consume_enum_choice(column_type[i + 1 :])
            choices.append(f"({value}, {name})")
            while remain[0] != ")":
                name, value, remain = self.consume_enum_choice(remain[2:])
                choices.append(f"({value}, {name})")
            param = self.merge_params(param, f"choices=[{', '.join(choices)}]")
            yield f"models.{typ}Field({param})"
            return remain[1:]
        elif column_type.startswith(
            "Array"
        ):  # Array(Tuple(String, Enum8('a' = 1, 'b' = 2)))
            yield "models.ArrayField("
            remain = yield from self.inspect_field_type(column_type[6:])
            if param:
                yield f", {param}"
            yield ")"
            return remain[1:]
        elif column_type.startswith("Tuple"):  # Tuple(String, Enum8('a' = 1, 'b' = 2))
            yield "models.TupleField(["
            remain = yield from self.inspect_field_type(column_type[6:])
            while remain[0] == ",":
                yield ", "
                remain = yield from self.inspect_field_type(remain[2:])
            yield "]"
            if param:
                yield f", {param}"
            yield ")"
            return remain[1:]
        elif column_type.startswith("Map"):  # Map(String, Int8)
            yield "models.MapField("
            remain = yield from self.inspect_field_type(column_type[4:])
            yield ", "
            remain = yield from self.inspect_field_type(remain[2:])
            if param:
                yield f", {param}"
            yield ")"
            return remain[1:]
        elif column_type.startswith("Object('json')"):
            yield f"models.JSONField({param})"
            return column_type[14:]

        i = 0
        length = len(column_type)
        while i < length and column_type[i].isalnum():
            i += 1
        yield f"models.{column_type[:i]}Field({param})"
        return column_type[i:]

    def consume_enum_choice(self, s):  # 'a' = 1
        has_bytes = False
        i = 1
        while True:
            if s[i] == "\\":  # escape char
                if s[i + 1] == "x":
                    has_bytes = True
                i += 2
                continue
            if s[i] == "'":
                break
            i += 1
        i += 1
        name = s[:i]
        # try decoding bytes to utf8 string.
        if has_bytes:
            try:
                decoded = eval(f"b{name}.decode('utf-8')")
            except UnicodeDecodeError:
                name = f"b{name}"
            else:
                name = repr(decoded)

        i += 3
        j = i
        while s[i].isdigit():
            i += 1
        value = s[j:i]
        return name, value, s[i:]

    def merge_params(self, *params):
        return ", ".join(filter(None, params))
