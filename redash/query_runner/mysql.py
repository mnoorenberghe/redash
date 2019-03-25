import logging
import os
import threading

from redash.query_runner import (
    TYPE_FLOAT,
    TYPE_INTEGER,
    TYPE_DATETIME,
    TYPE_STRING,
    TYPE_DATE,
    BaseSQLQueryRunner,
    InterruptException,
    register,
)
from redash.settings import parse_boolean
from redash.utils import json_dumps, json_loads

try:
    import MySQLdb

    enabled = True
except ImportError:
    enabled = False

logger = logging.getLogger(__name__)
types_map = {
    0: TYPE_FLOAT,
    1: TYPE_INTEGER,
    2: TYPE_INTEGER,
    3: TYPE_INTEGER,
    4: TYPE_FLOAT,
    5: TYPE_FLOAT,
    7: TYPE_DATETIME,
    8: TYPE_INTEGER,
    9: TYPE_INTEGER,
    10: TYPE_DATE,
    12: TYPE_DATETIME,
    15: TYPE_STRING,
    16: TYPE_INTEGER,
    246: TYPE_FLOAT,
    253: TYPE_STRING,
    254: TYPE_STRING,
}


class Result(object):
    def __init__(self):
        pass


class Mysql(BaseSQLQueryRunner):
    noop_query = "SELECT 1"
    sample_query = "SELECT * FROM {table} LIMIT 1"

    @classmethod
    def configuration_schema(cls):
        show_ssl_settings = parse_boolean(
            os.environ.get("MYSQL_SHOW_SSL_SETTINGS", "true")
        )

        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "user": {"type": "string"},
                "passwd": {"type": "string", "title": "Password"},
                "db": {"type": "string", "title": "Database name"},
                "port": {"type": "number", "default": 3306},
                "toggle_table_string": {
                    "type": "string",
                    "title": "Toggle Table String",
                    "default": "_v",
                    "info": "This string will be used to toggle visibility of tables in the schema browser when editing a query in order to remove non-useful tables from sight.",
                },
                "samples": {"type": "boolean", "title": "Show Data Samples"},
            },
            "order": ["host", "port", "user", "passwd", "db"],
            "required": ["db"],
            "secret": ["passwd"],
        }

        if show_ssl_settings:
            schema["properties"].update(
                {
                    "use_ssl": {"type": "boolean", "title": "Use SSL"},
                    "ssl_cacert": {
                        "type": "string",
                        "title": "Path to CA certificate file to verify peer against (SSL)",
                    },
                    "ssl_cert": {
                        "type": "string",
                        "title": "Path to client certificate file (SSL)",
                    },
                    "ssl_key": {
                        "type": "string",
                        "title": "Path to private key file (SSL)",
                    },
                }
            )

        return schema

    @classmethod
    def name(cls):
        return "MySQL"

    @classmethod
    def enabled(cls):
        return enabled

    def _connection(self):
        params = dict(
            host=self.configuration.get("host", ""),
            user=self.configuration.get("user", ""),
            passwd=self.configuration.get("passwd", ""),
            db=self.configuration["db"],
            port=self.configuration.get("port", 3306),
            charset="utf8",
            use_unicode=True,
            connect_timeout=60,
        )

        ssl_options = self._get_ssl_parameters()

        if ssl_options:
            params["ssl"] = ssl_options

        connection = MySQLdb.connect(**params)

        return connection

    def _get_tables(self, schema):
        query = """
        SELECT col.table_schema as table_schema,
               col.table_name as table_name,
               col.column_name as column_name,
               col.column_type as column_type
        FROM `information_schema`.`columns` col
        WHERE col.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys');
        """

        results, error = self.run_query(query, None)

        if error is not None:
            raise Exception("Failed getting schema.")

        results = json_loads(results)

        for row in results["rows"]:
            if row["table_schema"] != self.configuration["db"]:
                table_name = "{}.{}".format(row["table_schema"], row["table_name"])
            else:
                table_name = row["table_name"]

            if table_name not in schema:
                schema[table_name] = {"name": table_name, "columns": [], "metadata": []}

            schema[table_name]["columns"].append(row["column_name"])
            schema[table_name]["metadata"].append(
                {"name": row["column_name"], "type": row["column_type"]}
            )

        return list(schema.values())

    def run_query(self, query, user):
        ev = threading.Event()
        thread_id = ""
        r = Result()
        t = None
        try:
            connection = self._connection()
            thread_id = connection.thread_id()
            t = threading.Thread(
                target=self._run_query, args=(query, user, connection, r, ev)
            )
            t.start()
            while not ev.wait(1):
                pass
        except (KeyboardInterrupt, InterruptException):
            error = self._cancel(thread_id)
            t.join()
            r.json_data = None
            r.error = "Query cancelled by user."
            if error is not None:
                r.error = error

        return r.json_data, r.error

    def _run_query(self, query, user, connection, r, ev):
        try:
            cursor = connection.cursor()
            logger.debug("MySQL running query: %s", query)
            cursor.execute(query)

            data = cursor.fetchall()
            desc = cursor.description

            while cursor.nextset():
                if cursor.description is not None:
                    data = cursor.fetchall()
                    desc = cursor.description

            # TODO - very similar to pg.py
            if desc is not None:
                columns = self.fetch_columns(
                    [(i[0], types_map.get(i[1], None)) for i in desc]
                )
                rows = [
                    dict(zip((column["name"] for column in columns), row))
                    for row in data
                ]

                data = {"columns": columns, "rows": rows}
                r.json_data = json_dumps(data)
                r.error = None
            else:
                r.json_data = None
                r.error = "No data was returned."

            cursor.close()
        except MySQLdb.Error as e:
            if cursor:
                cursor.close()
            r.json_data = None
            r.error = e.args[1]
        finally:
            ev.set()
            if connection:
                connection.close()

    def _get_ssl_parameters(self):
        if not self.configuration.get("use_ssl"):
            return None

        ssl_params = {}

        if self.configuration.get("use_ssl"):
            config_map = {"ssl_cacert": "ca", "ssl_cert": "cert", "ssl_key": "key"}
            for key, cfg in config_map.items():
                val = self.configuration.get(key)
                if val:
                    ssl_params[cfg] = val

        return ssl_params

    def _cancel(self, thread_id):
        connection = None
        cursor = None
        error = None

        try:
            connection = self._connection()
            cursor = connection.cursor()
            query = "KILL %d" % (thread_id)
            logging.debug(query)
            cursor.execute(query)
        except MySQLdb.Error as e:
            if cursor:
                cursor.close()
            error = e.args[1]
        finally:
            if connection:
                connection.close()

        return error


class RDSMySQL(Mysql):
    @classmethod
    def name(cls):
        return "MySQL (Amazon RDS)"

    @classmethod
    def type(cls):
        return "rds_mysql"

    @classmethod
    def configuration_schema(cls):
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "user": {"type": "string"},
                "passwd": {"type": "string", "title": "Password"},
                "db": {"type": "string", "title": "Database name"},
                "port": {"type": "number", "default": 3306},
                "use_ssl": {"type": "boolean", "title": "Use SSL"},
            },
            "order": ["host", "port", "user", "passwd", "db"],
            "required": ["db", "user", "passwd", "host"],
            "secret": ["passwd"],
        }

    def _get_ssl_parameters(self):
        if self.configuration.get("use_ssl"):
            ca_path = os.path.join(
                os.path.dirname(__file__), "./files/rds-combined-ca-bundle.pem"
            )
            return {"ca": ca_path}

        return None


register(Mysql)
register(RDSMySQL)
