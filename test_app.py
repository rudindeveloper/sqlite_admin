#!/usr/bin/env python3
"""
Тесты для SQLite Web Admin (DatabaseManager и HTTP API).
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from app import AdminHandler, DatabaseManager


class TestDatabaseManager(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_file.close()
        self.db = DatabaseManager(self.temp_file.name)

        # Создаем тестовую схему и данные
        with self.db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE,
                    age INTEGER,
                    avatar BLOB
                );
            """)
            cur.execute("CREATE INDEX idx_users_age ON users(age);")
            cur.execute("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER,
                    amount REAL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            """)
            for i in range(1, 106):
                cur.execute(
                    "INSERT INTO users (name, email, age, avatar) VALUES (?, ?, ?, ?)",
                    (f"User_{i}", f"user{i}@example.com", 20 + (i % 30), b"\x00\x01\x02" if i == 1 else None),
                )
            conn.commit()

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.remove(self.temp_file.name)

    def test_list_tables(self):
        tables = self.db.list_tables()
        table_names = [t["name"] for t in tables]
        self.assertIn("users", table_names)
        self.assertIn("orders", table_names)

        users_item = next(t for t in tables if t["name"] == "users")
        self.assertEqual(users_item["count"], 105)

    def test_get_table_schema(self):
        schema = self.db.get_table_schema("users")
        self.assertEqual(schema["name"], "users")
        self.assertIn("CREATE TABLE users", schema["ddl"])

        cols = {c["name"]: c for c in schema["columns"]}
        self.assertIn("id", cols)
        self.assertTrue(cols["id"]["pk"])
        self.assertTrue(cols["name"]["notnull"])

        index_names = [idx["name"] for idx in schema["indexes"]]
        self.assertIn("idx_users_age", index_names)

    def test_get_table_data_pagination(self):
        page1 = self.db.get_table_data("users", page=1, page_size=10)
        self.assertEqual(len(page1["rows"]), 10)
        self.assertEqual(page1["total"], 105)
        self.assertEqual(page1["page"], 1)

        # Проверка BLOB формата
        first_row = page1["rows"][0]
        self.assertTrue(any("<BLOB" in str(v) for v in first_row))

        page11 = self.db.get_table_data("users", page=11, page_size=10)
        self.assertEqual(len(page11["rows"]), 5)

    def test_get_table_data_sorting(self):
        res = self.db.get_table_data("users", page=1, page_size=5, sort_col="id", sort_dir="DESC")
        first_id = res["rows"][0][0]
        self.assertEqual(first_id, 105)

    def test_execute_query_select(self):
        res = self.db.execute_query("SELECT id, name FROM users WHERE id = 1")
        self.assertIn("columns", res)
        self.assertEqual(res["columns"], ["id", "name"])
        self.assertEqual(res["rows"], [[1, "User_1"]])

    def test_execute_query_update(self):
        res = self.db.execute_query("UPDATE users SET age = 99 WHERE id = 1")
        self.assertIn("message", res)
        self.assertEqual(res["rowcount"], 1)

        check = self.db.execute_query("SELECT age FROM users WHERE id = 1")
        self.assertEqual(check["rows"][0][0], 99)

    def test_execute_query_error(self):
        res = self.db.execute_query("SELECT * FROM non_existing_table")
        self.assertIn("error", res)


class TestHttpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.temp_file.close()
        cls.db = DatabaseManager(cls.temp_file.name)
        with cls.db.get_connection() as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT);")
            conn.execute("INSERT INTO items (title) VALUES ('Item A'), ('Item B');")
            conn.commit()

        AdminHandler.db = cls.db
        # Привязываем к случайному свободному порту (порт 0)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AdminHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if os.path.exists(cls.temp_file.name):
            os.remove(cls.temp_file.name)

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
            return resp.status, content_type, content

    def _post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content)

    def test_get_root_html(self):
        status, content_type, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("<title>SQLite Admin</title>", html)

    def test_api_info(self):
        status, _, content = self._get("/api/info")
        self.assertEqual(status, 200)
        data = json.loads(content)
        self.assertIn("db_name", data)
        self.assertIn("size_human", data)

    def test_api_tables(self):
        status, _, content = self._get("/api/tables")
        self.assertEqual(status, 200)
        data = json.loads(content)
        self.assertEqual(data["tables"][0]["name"], "items")

    def test_api_table_data(self):
        status, _, content = self._get("/api/table/items/data?page=1&page_size=10")
        self.assertEqual(status, 200)
        data = json.loads(content)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["total"], 2)

    def test_api_query(self):
        status, data = self._post("/api/query", {"sql": "SELECT COUNT(*) FROM items"})
        self.assertEqual(status, 200)
        self.assertEqual(data["rows"][0][0], 2)

    def test_font_geologica_present(self):
        status, _, html = self._get("/")
        self.assertIn('value="Geologica"', html)

    def test_password_protection_flow(self):
        try:
            # Включаем пароль
            AdminHandler.password = "supersecret"
            AdminHandler.sessions.clear()

            # 1. Запрос к API без авторизации -> 401
            url = f"http://127.0.0.1:{self.port}/api/tables"
            try:
                urllib.request.urlopen(url)
                self.fail("Ожидался HTTP 401 Unauthorized")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 2. Неверный пароль -> 401
            login_url = f"http://127.0.0.1:{self.port}/api/login"
            req = urllib.request.Request(
                login_url,
                data=json.dumps({"password": "wrong"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req)
                self.fail("Ожидался HTTP 401 Unauthorized")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 3. Верный пароль -> 200 и Set-Cookie
            req = urllib.request.Request(
                login_url,
                data=json.dumps({"password": "supersecret"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                cookie_header = resp.headers.get("Set-Cookie")
                self.assertIn("sqlite_admin_session=", cookie_header)
                token = cookie_header.split(";")[0]

            # 4. Запрос с полученной кукой -> 200 OK
            req = urllib.request.Request(url, headers={"Cookie": token})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["tables"][0]["name"], "items")

            # 5. Выход -> очистка
            logout_url = f"http://127.0.0.1:{self.port}/api/logout"
            req = urllib.request.Request(logout_url, data=b"{}", headers={"Content-Type": "application/json", "Cookie": token})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)

            # 6. Повторный запрос со старой кукой -> 401
            req = urllib.request.Request(url, headers={"Cookie": token})
            try:
                urllib.request.urlopen(req)
                self.fail("Ожидался HTTP 401 Unauthorized после выхода")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)
        finally:
            AdminHandler.password = None
            AdminHandler.sessions.clear()


if __name__ == "__main__":
    unittest.main()

