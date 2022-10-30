import re

from django.db.backends.base.introspection import (
    BaseDatabaseIntrospection, FieldInfo, TableInfo,
)
from django.utils.functional import cached_property

constraint_pattern = re.compile(
    r'CONSTRAINT (`)?((?(1)(?:[^\\`]|\\.)+|\S+))(?(1)`|) (CHECK .+?),?\n'
)
index_pattern = re.compile(
    r'INDEX (`)?((?(1)(?:[^\\`]|\\.)+|\S+))(?(1)`|) (.+? TYPE ([a-zA-Z_][0-9a-zA-Z_]*)\(.+?\) GRANULARITY \d+)'
)


class DatabaseIntrospection(BaseDatabaseIntrospection):
    # Maps type codes to Django Field types.
    data_types_reverse = {
        # 'String': 'BinaryField',
        # The best type for String is BinaryField, but sometimes you may need TextField.
        'String': 'TextField',
        'Int64': 'BigIntegerField',
        'Int16': 'SmallIntegerField',
        'Int32': 'IntegerField',
        'UInt64': 'PositiveBigIntegerField',
        'UInt16': 'PositiveSmallIntegerField',
        'UInt32': 'PositiveIntegerField',
        'Float32': 'FloatField',
        'Float64': 'FloatField',
        'IPv4': 'GenericIPAddressField',
        'IPv6': 'GenericIPAddressField',
        'Date': 'DateField',
        'Date32': 'DateField',
        'DateTime': 'DateTimeField',
        'UUID': 'UUIDField',
    }

    ignored_tables = []

    def get_field_type(self, data_type, description):
        if data_type.startswith('FixedString'):  # FixedString(20)
            return 'CharField'
        elif data_type.startswith('DateTime64'):
            return 'DateTimeField'
        elif data_type.startswith('Decimal'):
            return 'DecimalField'
        elif data_type.startswith('Nullable'):  # Nullable(Int16)
            return self.get_field_type(data_type[9:-1], description)
        return super().get_field_type(data_type, description)

    def get_table_list(self, cursor):
        """Return a list of table and view names in the current database."""
        cursor.execute("""
            SELECT table_name,
            CASE table_type WHEN 2 THEN 'v' ELSE 't' END
            FROM INFORMATION_SCHEMA.TABLES
            WHERE table_catalog = currentDatabase()
            AND table_type IN (1, 2)
        """)
        return [TableInfo(*row) for row in cursor.fetchall() if row[0] not in self.ignored_tables]

    def get_table_description(self, cursor, table_name):
        """
        Return a description of the table.
        """
        # Query the INFORMATION_SCHEMA.COLUMNS table.
        cursor.execute("""
            SELECT column_name, data_type, NULL, character_maximum_length,
            coalesce(numeric_precision, datetime_precision),
            numeric_scale, is_nullable, column_default, NULL
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE table_catalog = currentDatabase() AND table_name = %s
        """, [table_name])
        return [
            FieldInfo(*line)
            for line in cursor.fetchall()
        ]

    def get_constraints(self, cursor, table_name):
        """
        Retrieve any constraints and indexes.
        """
        constraints = {}
        # No way to get structured data, parse from SHOW CREATE TABLE.
        # https://clickhouse.com/docs/en/sql-reference/statements/show#show-create-table
        cursor.execute('SHOW CREATE TABLE "%s"' % table_name)
        table_sql, = cursor.fetchone()
        for backtick, name, definition in constraint_pattern.findall(table_sql):
            constraints[name] = {
                "columns": [],
                "primary_key": False,
                "unique": False,
                "foreign_key": None,
                "check": True,
                "index": False,
                "definition": definition,
                "options": None,
            }

        for backtick, name, definition, type_ in index_pattern.findall(table_sql):
            constraints[name] = {
                "columns": [],
                "orders": [],
                "primary_key": False,
                "unique": False,
                "foreign_key": None,
                "check": False,
                "index": True,
                "type": type_,
                "definition": definition,
                "options": None,
            }
        return constraints

    @cached_property
    def settings(self) -> set:
        """
        Get all available settings.
        """
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT name from system.settings")
            rows = cursor.fetchall()
        return {row[0] for row in rows}
