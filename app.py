#!/usr/bin/env python3
"""
SQLite Web Admin - легкая веб-админка для SQLite на стандартной библиотеке Python.
Запуск: python app.py [путь_к_базе.db] [--port 8080] [--host 127.0.0.1]
"""

import argparse
import contextlib
import hmac
import html
import http.cookies
import http.server
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        if not os.path.exists(self.db_path):
            # Создаем пустую базу, если файл не существует
            conn = sqlite3.connect(self.db_path)
            conn.close()

    @contextlib.contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def list_tables(self):
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, type 
                FROM sqlite_master 
                WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' 
                ORDER BY type, name;
            """)
            items = []
            for row in cur.fetchall():
                name, obj_type = row["name"], row["type"]
                count = None
                if obj_type == "table":
                    try:
                        c_cur = conn.cursor()
                        # Безопасное экранирование имени таблицы через кавычки
                        escaped = name.replace('"', '""')
                        c_cur.execute(f'SELECT COUNT(*) FROM "{escaped}"')
                        count = c_cur.fetchone()[0]
                    except Exception:
                        count = None
                items.append({"name": name, "type": obj_type, "count": count})
            return items

    def get_table_schema(self, table_name: str):
        tables = [t["name"] for t in self.list_tables()]
        if table_name not in tables:
            raise ValueError(f"Таблица или представление '{table_name}' не найдены.")

        escaped = table_name.replace('"', '""')
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table_name,))
            ddl_row = cur.fetchone()
            ddl = ddl_row["sql"] if ddl_row and ddl_row["sql"] else f"-- DDL отсутствует для {table_name}"

            cur.execute(f'PRAGMA table_info("{escaped}")')
            columns = [
                {
                    "cid": r["cid"],
                    "name": r["name"],
                    "type": r["type"],
                    "notnull": bool(r["notnull"]),
                    "dflt_value": r["dflt_value"],
                    "pk": bool(r["pk"]),
                }
                for r in cur.fetchall()
            ]

            cur.execute(f'PRAGMA foreign_key_list("{escaped}")')
            fks = [
                {
                    "id": r["id"],
                    "seq": r["seq"],
                    "table": r["table"],
                    "from": r["from"],
                    "to": r["to"],
                }
                for r in cur.fetchall()
            ]

            cur.execute(f'PRAGMA index_list("{escaped}")')
            indexes = [
                {"name": r["name"], "unique": bool(r["unique"]), "origin": r["origin"]}
                for r in cur.fetchall()
            ]

            return {
                "name": table_name,
                "ddl": ddl,
                "columns": columns,
                "foreign_keys": fks,
                "indexes": indexes,
            }

    def get_table_data(self, table_name: str, page: int = 1, page_size: int = 50, sort_col: str = None, sort_dir: str = "ASC"):
        tables = [t["name"] for t in self.list_tables()]
        if table_name not in tables:
            raise ValueError(f"Таблица или представление '{table_name}' не найдены.")

        page = max(1, page)
        page_size = max(1, min(page_size, 1000))
        offset = (page - 1) * page_size
        escaped_table = table_name.replace('"', '""')

        with self.get_connection() as conn:
            cur = conn.cursor()
            # Общее количество строк
            total = 0
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{escaped_table}"')
                total = cur.fetchone()[0]
            except Exception:
                pass

            order_clause = ""
            if sort_col:
                # Проверим, что sort_col существует среди колонок
                cur.execute(f'PRAGMA table_info("{escaped_table}")')
                valid_cols = [r["name"] for r in cur.fetchall()]
                if sort_col in valid_cols:
                    direction = "DESC" if str(sort_dir).upper() == "DESC" else "ASC"
                    escaped_col = sort_col.replace('"', '""')
                    order_clause = f' ORDER BY "{escaped_col}" {direction}'

            cur.execute(f'SELECT * FROM "{escaped_table}"{order_clause} LIMIT ? OFFSET ?', (page_size, offset))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = []
            for r in cur.fetchall():
                row_vals = []
                for val in r:
                    if isinstance(val, bytes):
                        row_vals.append(f"<BLOB {len(val)}B: 0x{val[:16].hex()}...>")
                    else:
                        row_vals.append(val)
                rows.append(row_vals)

            return {
                "columns": cols,
                "rows": rows,
                "page": page,
                "page_size": page_size,
                "total": total,
            }

    def execute_query(self, sql: str):
        sql = sql.strip()
        if not sql:
            return {"error": "Пустой SQL запрос."}

        start = time.perf_counter()
        with self.get_connection() as conn:
            cur = conn.cursor()
            try:
                # executescript vs execute: если несколько выражений, разрешаем выполнение
                # Но если есть SELECT - удобнее execute, чтобы получить cursor.description
                if ";" in sql.rstrip(";"):
                    cur.executescript(sql)
                    conn.commit()
                    elapsed = (time.perf_counter() - start) * 1000
                    return {
                        "message": "Скрипт успешно выполнен.",
                        "elapsed_ms": round(elapsed, 2),
                        "rowcount": conn.total_changes,
                    }
                else:
                    cur.execute(sql)
                    elapsed = (time.perf_counter() - start) * 1000
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = []
                        # Ограничим вывод 1000 строками для предотвращения переполнения памяти
                        raw_rows = cur.fetchmany(1000)
                        for r in raw_rows:
                            row_vals = []
                            for val in r:
                                if isinstance(val, bytes):
                                    row_vals.append(f"<BLOB {len(val)}B: 0x{val[:16].hex()}...>")
                                else:
                                    row_vals.append(val)
                            rows.append(row_vals)
                        return {
                            "columns": cols,
                            "rows": rows,
                            "count": len(rows),
                            "elapsed_ms": round(elapsed, 2),
                        }
                    else:
                        conn.commit()
                        return {
                            "message": f"Запрос выполнен. Изменено строк: {cur.rowcount}.",
                            "elapsed_ms": round(elapsed, 2),
                            "rowcount": cur.rowcount,
                        }
            except sqlite3.Error as e:
                return {"error": str(e)}


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SQLite Admin</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link id="primaryGoogleFont" rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap">
  <style>
    :root {
      --font-main: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-size: 14px;
      --font-weight: 400;
      --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --radius: 8px;
    }

    /* Светлая тема по умолчанию */
    body, body[data-theme="light"] {
      --bg: #f8fafc;
      --surface: #ffffff;
      --surface-2: #f1f5f9;
      --surface-3: #e2e8f0;
      --border: #e2e8f0;
      --border-strong: #cbd5e1;
      --text: #0f172a;
      --text-muted: #64748b;
      --primary: #0284c7;
      --primary-hover: #0369a1;
      --accent: #4f46e5;
      --danger: #ef4444;
      --success: #10b981;
      --sql-bg: #ffffff;
      --sql-text: #0f172a;
      --th-bg: #f8fafc;
      --th-text: #1e293b;
      --ddl-bg: #f8fafc;
      --ddl-text: #0369a1;
      --active-tab-bg: rgba(2, 132, 199, 0.08);
      --table-hover: rgba(2, 132, 199, 0.04);
      --empty-icon-opacity: 0.4;
    }

    /* Тёмная тема */
    body[data-theme="dark"] {
      --bg: #0b0f19;
      --surface: #111827;
      --surface-2: #1f2937;
      --surface-3: #374151;
      --border: #2d3748;
      --border-strong: #475569;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --primary: #38bdf8;
      --primary-hover: #0ea5e9;
      --accent: #6366f1;
      --danger: #f87171;
      --success: #34d399;
      --sql-bg: #060911;
      --sql-text: #38bdf8;
      --th-bg: #192231;
      --th-text: #f3f4f6;
      --ddl-bg: #060911;
      --ddl-text: #7dd3fc;
      --active-tab-bg: rgba(56, 189, 248, 0.12);
      --table-hover: rgba(255, 255, 255, 0.03);
      --empty-icon-opacity: 0.6;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font-main);
      font-size: var(--font-size);
      font-weight: var(--font-weight);
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
      transition: background-color 0.2s, color 0.2s;
    }

    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 8px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-shrink: 0;
    }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .header-right { display: flex; align-items: center; gap: 8px; }
    .logo {
      font-weight: 700;
      font-size: 16px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--primary);
      letter-spacing: -0.3px;
    }
    .db-badge {
      background: var(--surface-2);
      border: 1px solid var(--border);
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 12px;
      color: var(--text-muted);
      max-width: 320px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .db-badge strong { color: var(--text); }

    /* Панель управления шрифтами */
    .font-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .font-controls label {
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 12px;
      color: var(--text-muted);
    }
    .font-controls select {
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 4px 8px;
      border-radius: var(--radius);
      font-size: 12px;
      font-family: inherit;
      outline: none;
      cursor: pointer;
    }
    .font-controls select:focus { border-color: var(--primary); }

    .layout {
      display: flex;
      flex: 1;
      overflow: hidden;
    }

    /* Боковая панель */
    aside {
      width: 270px;
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
    }
    .sidebar-search {
      padding: 10px;
      border-bottom: 1px solid var(--border);
    }
    .sidebar-search input {
      width: 100%;
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 10px;
      border-radius: var(--radius);
      font-size: 13px;
      font-family: inherit;
      outline: none;
    }
    .sidebar-search input:focus { border-color: var(--primary); }
    .table-list {
      flex: 1;
      overflow-y: auto;
      list-style: none;
      padding: 6px 0;
    }
    .table-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 14px;
      cursor: pointer;
      user-select: none;
      transition: background 0.15s, color 0.15s;
      color: var(--text-muted);
    }
    .table-item:hover { background: var(--surface-2); color: var(--text); }
    .table-item.active {
      background: var(--active-tab-bg);
      color: var(--primary);
      font-weight: 600;
      border-left: 3px solid var(--primary);
    }
    .table-name {
      display: flex;
      align-items: center;
      gap: 8px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .table-pill {
      font-size: 11px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      padding: 2px 6px;
      border-radius: 10px;
      color: var(--text-muted);
    }

    /* Основная панель */
    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg);
    }

    /* SQL панель */
    .sql-panel {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex-shrink: 0;
    }
    .sql-editor-container { position: relative; }
    textarea#sqlQuery {
      width: 100%;
      height: 70px;
      background: var(--sql-bg);
      border: 1px solid var(--border-strong);
      border-radius: var(--radius);
      color: var(--sql-text);
      font-family: var(--font-mono);
      font-size: 13px;
      padding: 10px;
      resize: vertical;
      outline: none;
      line-height: 1.45;
    }
    textarea#sqlQuery:focus { border-color: var(--primary); }
    .sql-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .btn-group { display: flex; gap: 8px; align-items: center; }
    button {
      background: var(--surface);
      border: 1px solid var(--border-strong);
      color: var(--text);
      padding: 6px 14px;
      border-radius: var(--radius);
      font-size: 13px;
      font-weight: 500;
      font-family: inherit;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }
    button:hover { background: var(--surface-2); }
    button.primary {
      background: var(--primary);
      color: #ffffff;
      border-color: var(--primary);
      font-weight: 600;
    }
    button.primary:hover { background: var(--primary-hover); }
    .hint { font-size: 12px; color: var(--text-muted); }

    /* Табы */
    .tabs-bar {
      display: flex;
      align-items: center;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 16px;
      gap: 4px;
      flex-shrink: 0;
    }
    .tab {
      padding: 10px 16px;
      cursor: pointer;
      color: var(--text-muted);
      border-bottom: 2px solid transparent;
      user-select: none;
      font-weight: 500;
    }
    .tab:hover { color: var(--text); }
    .tab.active {
      color: var(--primary);
      border-bottom-color: var(--primary);
      font-weight: 600;
    }

    /* Контент вкладок */
    .content-area {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      padding: 14px 16px;
    }
    .tab-content {
      display: none;
      flex: 1;
      flex-direction: column;
      overflow: hidden;
    }
    .tab-content.active { display: flex; }

    /* Таблица данных */
    .table-container {
      flex: 1;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: inherit;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      background: var(--th-bg);
      color: var(--th-text);
      text-align: left;
      padding: 9px 12px;
      border-bottom: 1px solid var(--border);
      border-right: 1px solid var(--border);
      cursor: pointer;
      user-select: none;
      font-weight: 600;
      z-index: 1;
    }
    th:hover { background: var(--surface-2); }
    td {
      padding: 7px 12px;
      border-bottom: 1px solid var(--border);
      border-right: 1px solid var(--border);
      color: var(--text);
      max-width: 320px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    tr:hover td { background: var(--table-hover); }
    .null-val {
      color: var(--text-muted);
      font-style: italic;
      font-size: 11px;
      background: var(--surface-2);
      padding: 1px 4px;
      border-radius: 3px;
    }

    /* Пагинация */
    .pagination-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 0 0 0;
      flex-shrink: 0;
    }
    .pagination-bar select {
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 4px 8px;
      border-radius: var(--radius);
      font-size: 13px;
      font-family: inherit;
    }

    /* Схема DDL */
    .ddl-box {
      background: var(--ddl-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px;
      font-family: var(--font-mono);
      font-size: 13px;
      color: var(--ddl-text);
      overflow: auto;
      max-height: 220px;
      margin-bottom: 16px;
      line-height: 1.45;
    }
    .schema-title {
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 8px;
      color: var(--text);
    }
    .badge-pk {
      background: rgba(245, 158, 11, 0.15);
      border: 1px solid rgba(245, 158, 11, 0.4);
      color: #b45309;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
    }
    body[data-theme="dark"] .badge-pk {
      color: #fbbf24;
      background: rgba(245, 158, 11, 0.2);
    }

    /* Сообщения */
    .alert {
      padding: 10px 14px;
      border-radius: var(--radius);
      font-size: 13px;
      margin-bottom: 10px;
    }
    .alert-error {
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid var(--danger);
      color: var(--danger);
    }
    .alert-success {
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid var(--success);
      color: var(--success);
    }
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100%;
      color: var(--text-muted);
      gap: 8px;
    }

    /* Модальное окно авторизации */
    .login-overlay {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.65);
      backdrop-filter: blur(5px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
    }
    .login-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 30px 26px;
      width: 100%;
      max-width: 360px;
      box-shadow: 0 15px 35px -5px rgba(0, 0, 0, 0.2);
    }
    .login-logo {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      color: var(--primary);
      margin-bottom: 8px;
    }
    .login-desc {
      text-align: center;
      color: var(--text-muted);
      font-size: 13px;
      margin-bottom: 20px;
    }
    .login-card input[type="password"] {
      width: 100%;
      background: var(--surface-2);
      border: 1px solid var(--border-strong);
      color: var(--text);
      padding: 9px 12px;
      border-radius: var(--radius);
      font-size: 14px;
      margin-bottom: 14px;
      outline: none;
      box-sizing: border-box;
    }
    .login-card input[type="password"]:focus {
      border-color: var(--primary);
    }
  </style>
</head>
<body data-theme="light">
  <header>
    <div class="header-left">
      <div class="logo">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path>
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>
        </svg>
        SQLite Admin
      </div>
      <div class="db-badge" id="dbInfo" title="Файл базы данных">Загрузка...</div>
    </div>
    <div class="header-right">
      <div class="font-controls">
        <label title="Шрифт (fonts.google.com)">
          <span style="font-weight:bold;">Aa</span>
          <select id="fontSelect" onchange="applyFont(this.value)">
            <option value="Inter">Inter (Google Fonts)</option>
            <option value="Geologica">Geologica (Google Fonts)</option>
            <option value="Roboto">Roboto (Google Fonts)</option>
            <option value="Open Sans">Open Sans (Google Fonts)</option>
            <option value="Outfit">Outfit (Google Fonts)</option>
            <option value="Montserrat">Montserrat (Google Fonts)</option>
            <option value="JetBrains Mono">JetBrains Mono (Google Fonts)</option>
            <option value="Fira Code">Fira Code (Google Fonts)</option>
            <option value="Source Code Pro">Source Code Pro (Google Fonts)</option>
            <option value="system">Системный шрифт</option>
          </select>
        </label>
        <label title="Размер шрифта">
          <span>Размер:</span>
          <select id="fontSizeSelect" onchange="applyFontSize(this.value)">
            <option value="12">12px</option>
            <option value="13">13px</option>
            <option value="14" selected>14px</option>
            <option value="15">15px</option>
            <option value="16">16px</option>
            <option value="18">18px</option>
          </select>
        </label>
        <label title="Толщина / ширина шрифта (Weight)">
          <span>Толщина:</span>
          <select id="fontWeightSelect" onchange="applyFontWeight(this.value)">
            <option value="300">300 (Light)</option>
            <option value="400" selected>400 (Regular)</option>
            <option value="500">500 (Medium)</option>
            <option value="600">600 (Semi-Bold)</option>
            <option value="700">700 (Bold)</option>
          </select>
        </label>
        <button id="themeToggleBtn" onclick="toggleTheme()" title="Переключить тему (светлая / тёмная)">🌙 Тёмная</button>
        <button onclick="refreshAll()" title="Обновить список таблиц и текущее окно">↻ Обновить</button>
        <button id="logoutBtn" onclick="handleLogout()" title="Выйти из сессии" style="display: none;">Выйти</button>
      </div>
    </div>
  </header>

  <div class="layout">
    <aside>
      <div class="sidebar-search">
        <input type="text" id="searchTable" placeholder="Фильтр таблиц..." oninput="filterTables()">
      </div>
      <ul class="table-list" id="tableList">
        <li style="padding: 16px; color: var(--text-muted); text-align: center;">Загрузка...</li>
      </ul>
    </aside>

    <main>
      <!-- SQL Панель -->
      <div class="sql-panel">
        <div class="sql-editor-container">
          <textarea id="sqlQuery" placeholder="SELECT * FROM table LIMIT 50;  (или Ctrl+Enter для выполнения)"></textarea>
        </div>
        <div class="sql-actions">
          <div class="btn-group">
            <button class="primary" onclick="runSql()">▶ Выполнить</button>
            <button onclick="document.getElementById('sqlQuery').value = ''">Очистить</button>
          </div>
          <span class="hint">Совет: Ctrl + Enter для быстрого запуска</span>
        </div>
      </div>

      <!-- Вкладки -->
      <div class="tabs-bar">
        <div class="tab active" id="tabDataBtn" onclick="switchTab('data')">Данные</div>
        <div class="tab" id="tabSchemaBtn" onclick="switchTab('schema')">Схема & DDL</div>
        <div class="tab" id="tabSqlBtn" onclick="switchTab('sql')">Результат SQL <span id="sqlBadge" style="display:none;" class="table-pill">0</span></div>
      </div>

      <!-- Содержимое -->
      <div class="content-area">
        <div id="statusAlert"></div>

        <!-- Таб: Данные -->
        <div class="tab-content active" id="tabData">
          <div class="table-container" id="dataContainer">
            <div class="empty-state">Выберите таблицу слева для просмотра данных</div>
          </div>
          <div class="pagination-bar" id="paginationBar" style="display: none;">
            <div class="hint" id="pageInfo">Строки 0 - 0 из 0</div>
            <div class="btn-group">
              <label class="hint" style="display:flex; align-items:center; gap:6px;">
                Показывать:
                <select id="pageSizeSelect" onchange="changePageSize()">
                  <option value="25">25</option>
                  <option value="50" selected>50</option>
                  <option value="100">100</option>
                  <option value="500">500</option>
                </select>
              </label>
              <button id="btnPrevPage" onclick="prevPage()">← Назад</button>
              <button id="btnNextPage" onclick="nextPage()">Вперед →</button>
            </div>
          </div>
        </div>

        <!-- Таб: Схема -->
        <div class="tab-content" id="tabSchema">
          <div style="overflow-y: auto; flex: 1;">
            <div class="schema-title">DDL Создания таблицы</div>
            <pre class="ddl-box" id="ddlBox">-- DDL не загружен</pre>

            <div class="schema-title">Колонки</div>
            <div class="table-container" style="max-height: 280px; margin-bottom: 16px;">
              <table id="columnsTable">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Имя</th>
                    <th>Тип</th>
                    <th>NOT NULL</th>
                    <th>Default</th>
                    <th>PK</th>
                  </tr>
                </thead>
                <tbody id="columnsBody"></tbody>
              </table>
            </div>

            <div class="schema-title">Индексы</div>
            <div class="table-container" style="max-height: 180px;">
              <table id="indexesTable">
                <thead>
                  <tr>
                    <th>Имя индекса</th>
                    <th>Уникальный</th>
                    <th>Происхождение</th>
                  </tr>
                </thead>
                <tbody id="indexesBody"></tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- Таб: SQL Результат -->
        <div class="tab-content" id="tabSql">
          <div class="table-container" id="sqlResultContainer">
            <div class="empty-state">Запустите SQL-запрос для просмотра результата</div>
          </div>
        </div>
      </div>
    </main>
  </div>

  <div id="loginOverlay" class="login-overlay" style="display: none;">
    <div class="login-card">
      <div class="login-logo">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path>
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>
        </svg>
        <h3 style="margin: 0; font-size: 18px; font-weight: 700;">SQLite Admin</h3>
      </div>
      <p class="login-desc">Для доступа к базе данных введите пароль</p>
      <div id="loginError" class="alert alert-error" style="display: none; margin-bottom: 12px;"></div>
      <form id="loginForm" onsubmit="submitLogin(event)">
        <input type="password" id="loginPassword" placeholder="Пароль" autocomplete="current-password" required>
        <button type="submit" class="primary" style="width: 100%; justify-content: center; padding: 9px;">Войти</button>
      </form>
    </div>
  </div>

  <script>
    let state = {
      tables: [],
      currentTable: null,
      page: 1,
      pageSize: 50,
      total: 0,
      sortCol: null,
      sortDir: 'ASC',
      theme: 'light',
      font: 'Inter',
      fontSize: '14',
      fontWeight: '400'
    };

    // Управление шрифтами Google Fonts
    function applyFont(fontName) {
      state.font = fontName;
      localStorage.setItem('sqladmin_font', fontName);
      const sel = document.getElementById('fontSelect');
      if (sel) sel.value = fontName;

      if (fontName === 'system') {
        document.documentElement.style.setProperty('--font-main', '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif');
        return;
      }

      const linkId = 'gf-' + fontName.replace(/\s+/g, '-');
      if (!document.getElementById(linkId)) {
        const link = document.createElement('link');
        link.id = linkId;
        link.rel = 'stylesheet';
        link.href = `https://fonts.googleapis.com/css2?family=${encodeURIComponent(fontName)}:wght@300;400;500;600;700;800;900&display=swap`;
        document.head.appendChild(link);
      }
      document.documentElement.style.setProperty('--font-main', `'${fontName}', sans-serif`);
    }

    function applyFontSize(size) {
      state.fontSize = size;
      localStorage.setItem('sqladmin_fontSize', size);
      const sel = document.getElementById('fontSizeSelect');
      if (sel) sel.value = size;
      document.documentElement.style.setProperty('--font-size', size + 'px');
    }

    function applyFontWeight(weight) {
      state.fontWeight = weight;
      localStorage.setItem('sqladmin_fontWeight', weight);
      const sel = document.getElementById('fontWeightSelect');
      if (sel) sel.value = weight;
      document.documentElement.style.setProperty('--font-weight', weight);
    }

    // Управление темами (Светлая по умолчанию)
    function setTheme(theme) {
      state.theme = theme;
      localStorage.setItem('sqladmin_theme', theme);
      document.body.setAttribute('data-theme', theme);
      const btn = document.getElementById('themeToggleBtn');
      if (btn) {
        btn.textContent = theme === 'dark' ? '☀️ Светлая' : '🌙 Тёмная';
      }
    }

    function toggleTheme() {
      const nextTheme = state.theme === 'dark' ? 'light' : 'dark';
      setTheme(nextTheme);
    }

    function initTypographyAndTheme() {
      const savedTheme = localStorage.getItem('sqladmin_theme') || 'light';
      setTheme(savedTheme);

      const savedFont = localStorage.getItem('sqladmin_font') || 'Inter';
      applyFont(savedFont);

      const savedFontSize = localStorage.getItem('sqladmin_fontSize') || '14';
      applyFontSize(savedFontSize);

      const savedFontWeight = localStorage.getItem('sqladmin_fontWeight') || '400';
      applyFontWeight(savedFontWeight);
    }

    // Горячие клавиши
    document.getElementById('sqlQuery').addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        runSql();
      }
    });

    async function api(path, opts = {}) {
      try {
        const res = await fetch(path, opts);
        const data = await res.json();
        if (res.status === 401 && data.auth_required) {
          showLoginModal(true);
        }
        return data;
      } catch (err) {
        return { error: 'Сетевая ошибка: ' + err.message };
      }
    }

    function showLoginModal(show) {
      const overlay = document.getElementById('loginOverlay');
      overlay.style.display = show ? 'flex' : 'none';
      if (show) {
        const input = document.getElementById('loginPassword');
        input.value = '';
        setTimeout(() => input.focus(), 50);
      }
    }

    async function checkAuth() {
      const data = await api('/api/auth-status');
      if (data && data.auth_required) {
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) logoutBtn.style.display = data.logged_in ? 'inline-flex' : 'none';
        if (!data.logged_in) {
          showLoginModal(true);
          return false;
        }
      }
      return true;
    }

    async function submitLogin(e) {
      e.preventDefault();
      const password = document.getElementById('loginPassword').value;
      const errEl = document.getElementById('loginError');
      errEl.style.display = 'none';

      const res = await api('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password })
      });

      if (res && res.success) {
        showLoginModal(false);
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) logoutBtn.style.display = 'inline-flex';
        loadInfo();
        loadTables();
      } else {
        errEl.textContent = (res && res.error) ? res.error : 'Неверный пароль';
        errEl.style.display = 'block';
      }
    }

    async function handleLogout() {
      await api('/api/logout', { method: 'POST' });
      const logoutBtn = document.getElementById('logoutBtn');
      if (logoutBtn) logoutBtn.style.display = 'none';
      state.tables = [];
      state.currentTable = null;
      renderTableList();
      document.getElementById('dataContainer').innerHTML = '<div class="empty-state">Требуется авторизация</div>';
      showLoginModal(true);
    }

    function showAlert(msg, isError = true) {
      const el = document.getElementById('statusAlert');
      el.className = isError ? 'alert alert-error' : 'alert alert-success';
      el.textContent = msg;
      el.style.display = 'block';
      setTimeout(() => { el.style.display = 'none'; }, 5000);
    }

    function switchTab(name) {
      ['data', 'schema', 'sql'].forEach(t => {
        document.getElementById('tab' + capitalize(t)).classList.toggle('active', t === name);
        document.getElementById('tab' + capitalize(t) + 'Btn').classList.toggle('active', t === name);
      });
    }

    function capitalize(s) {
      return s.charAt(0).toUpperCase() + s.slice(1);
    }

    async function loadInfo() {
      const data = await api('/api/info');
      if (data.db_path) {
        document.getElementById('dbInfo').innerHTML = `База: <strong>${escapeHtml(data.db_name)}</strong> (${data.size_human})`;
        document.getElementById('dbInfo').title = data.db_path;
      }
    }

    async function loadTables() {
      const data = await api('/api/tables');
      if (data.error) {
        showAlert(data.error);
        return;
      }
      state.tables = data.tables || [];
      renderTableList();
      if (!state.currentTable && state.tables.length > 0) {
        selectTable(state.tables[0].name);
      }
    }

    function renderTableList() {
      const query = document.getElementById('searchTable').value.toLowerCase();
      const list = document.getElementById('tableList');
      list.innerHTML = '';

      const filtered = state.tables.filter(t => t.name.toLowerCase().includes(query));
      if (filtered.length === 0) {
        list.innerHTML = '<li style="padding:16px; color:var(--text-muted); text-align:center;">Таблицы не найдены</li>';
        return;
      }

      filtered.forEach(t => {
        const li = document.createElement('li');
        li.className = 'table-item' + (state.currentTable === t.name ? ' active' : '');
        li.onclick = () => selectTable(t.name);
        li.innerHTML = `
          <div class="table-name" title="${escapeHtml(t.name)}">
            <span style="opacity:0.6">${t.type === 'view' ? '👁' : '▤'}</span>
            <span>${escapeHtml(t.name)}</span>
          </div>
          <span class="table-pill">${t.count !== null ? t.count : t.type}</span>
        `;
        list.appendChild(li);
      });
    }

    function filterTables() {
      renderTableList();
    }

    async function selectTable(name) {
      state.currentTable = name;
      state.page = 1;
      state.sortCol = null;
      renderTableList();

      document.getElementById('sqlQuery').value = `SELECT * FROM "${name}" LIMIT 50;`;

      await Promise.all([loadTableData(), loadTableSchema()]);
      switchTab('data');
    }

    async function loadTableData() {
      if (!state.currentTable) return;
      const params = new URLSearchParams({
        page: state.page,
        page_size: state.pageSize,
      });
      if (state.sortCol) {
        params.append('sort_col', state.sortCol);
        params.append('sort_dir', state.sortDir);
      }

      const res = await api(`/api/table/${encodeURIComponent(state.currentTable)}/data?` + params.toString());
      const container = document.getElementById('dataContainer');
      const pag = document.getElementById('paginationBar');

      if (res.error) {
        showAlert(res.error);
        container.innerHTML = `<div class="empty-state">${escapeHtml(res.error)}</div>`;
        pag.style.display = 'none';
        return;
      }

      state.total = res.total;
      renderDataTable(container, res.columns, res.rows, true);

      // Пагинация
      pag.style.display = 'flex';
      const start = res.total === 0 ? 0 : (res.page - 1) * res.page_size + 1;
      const end = Math.min(res.page * res.page_size, res.total);
      document.getElementById('pageInfo').textContent = `Строки ${start} - ${end} из ${res.total}`;
      document.getElementById('btnPrevPage').disabled = res.page <= 1;
      document.getElementById('btnNextPage').disabled = end >= res.total;
    }

    function renderDataTable(container, columns, rows, sortable = false) {
      if (!columns || columns.length === 0) {
        container.innerHTML = '<div class="empty-state">Нет данных для отображения</div>';
        return;
      }

      let html = '<table><thead><tr>';
      columns.forEach(col => {
        let sortIcon = '';
        if (sortable && state.sortCol === col) {
          sortIcon = state.sortDir === 'ASC' ? ' ↑' : ' ↓';
        }
        const onclickAttr = sortable ? `onclick="sortBy('${escapeHtml(col)}')"` : '';
        html += `<th ${onclickAttr}>${escapeHtml(col)}${sortIcon}</th>`;
      });
      html += '</tr></thead><tbody>';

      if (rows.length === 0) {
        html += `<tr><td colspan="${columns.length}" style="text-align:center; padding:24px; color:var(--text-muted)">Нет записей</td></tr>`;
      } else {
        rows.forEach(r => {
          html += '<tr>';
          r.forEach(val => {
            if (val === null) {
              html += '<td><span class="null-val">NULL</span></td>';
            } else {
              html += `<td title="${escapeHtml(String(val))}">${escapeHtml(String(val))}</td>`;
            }
          });
          html += '</tr>';
        });
      }
      html += '</tbody></table>';
      container.innerHTML = html;
    }

    function sortBy(col) {
      if (state.sortCol === col) {
        state.sortDir = state.sortDir === 'ASC' ? 'DESC' : 'ASC';
      } else {
        state.sortCol = col;
        state.sortDir = 'ASC';
      }
      state.page = 1;
      loadTableData();
    }

    function prevPage() {
      if (state.page > 1) {
        state.page--;
        loadTableData();
      }
    }

    function nextPage() {
      if (state.page * state.pageSize < state.total) {
        state.page++;
        loadTableData();
      }
    }

    function changePageSize() {
      state.pageSize = parseInt(document.getElementById('pageSizeSelect').value, 10);
      state.page = 1;
      loadTableData();
    }

    async function loadTableSchema() {
      if (!state.currentTable) return;
      const res = await api(`/api/table/${encodeURIComponent(state.currentTable)}/schema`);
      if (res.error) {
        showAlert(res.error);
        return;
      }

      document.getElementById('ddlBox').textContent = res.ddl || '';

      // Колонки
      const colBody = document.getElementById('columnsBody');
      colBody.innerHTML = '';
      (res.columns || []).forEach(c => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${c.cid}</td>
          <td><strong>${escapeHtml(c.name)}</strong></td>
          <td><span style="color:var(--primary); font-weight:500;">${escapeHtml(c.type || 'ANY')}</span></td>
          <td>${c.notnull ? '✓' : ''}</td>
          <td>${c.dflt_value !== null ? escapeHtml(String(c.dflt_value)) : '<span class="null-val">NULL</span>'}</td>
          <td>${c.pk ? '<span class="badge-pk">PRIMARY KEY</span>' : ''}</td>
        `;
        colBody.appendChild(tr);
      });

      // Индексы
      const idxBody = document.getElementById('indexesBody');
      idxBody.innerHTML = '';
      if (!res.indexes || res.indexes.length === 0) {
        idxBody.innerHTML = '<tr><td colspan="3" style="text-align:center; color:var(--text-muted); padding:10px;">Индексов нет</td></tr>';
      } else {
        res.indexes.forEach(idx => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${escapeHtml(idx.name)}</td>
            <td>${idx.unique ? '✓ Уникальный' : 'Обычный'}</td>
            <td>${escapeHtml(idx.origin)}</td>
          `;
          idxBody.appendChild(tr);
        });
      }
    }

    async function runSql() {
      const sql = document.getElementById('sqlQuery').value.trim();
      if (!sql) return;

      const container = document.getElementById('sqlResultContainer');
      container.innerHTML = '<div class="empty-state">Выполнение...</div>';
      switchTab('sql');

      const res = await api('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql })
      });

      if (res.error) {
        showAlert(res.error, true);
        container.innerHTML = `<div class="alert alert-error" style="margin:16px;"><strong>Ошибка выполнения:</strong><br>${escapeHtml(res.error)}</div>`;
        return;
      }

      const badge = document.getElementById('sqlBadge');
      badge.style.display = 'inline-block';

      if (res.columns) {
        badge.textContent = `${res.count} строк (${res.elapsed_ms}мс)`;
        renderDataTable(container, res.columns, res.rows, false);
      } else {
        badge.textContent = `${res.elapsed_ms}мс`;
        container.innerHTML = `
          <div class="alert alert-success" style="margin:16px;">
            ${escapeHtml(res.message || 'Запрос успешно выполнен.')} (Время: ${res.elapsed_ms}мс)
          </div>
        `;
        loadTables();
        if (state.currentTable) {
          loadTableData();
        }
      }
    }

    async function refreshAll() {
      await loadInfo();
      await loadTables();
      if (state.currentTable) {
        await loadTableData();
        await loadTableSchema();
      }
      showAlert('Данные обновлены', false);
    }

    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    // Инициализация при старте страницы
    window.onload = async () => {
      initTypographyAndTheme();
      const ok = await checkAuth();
      if (ok) {
        loadInfo();
        loadTables();
      }
    };
  </script>
</body>
</html>
"""


class AdminHandler(http.server.BaseHTTPRequestHandler):
    db: DatabaseManager = None
    password: str = None
    sessions: set = set()

    def _get_session_token(self):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(cookie_header)
            if "sqlite_admin_session" in cookie:
                return cookie["sqlite_admin_session"].value
        except Exception:
            pass
        return None

    def _is_authenticated(self):
        if not self.password:
            return True
        token = self._get_session_token()
        return bool(token and token in self.sessions)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_content: str, status=200):
        body = html_content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)

        if path == "" or path == "/index.html":
            return self._send_html(HTML_PAGE)

        if path == "/api/auth-status":
            return self._send_json({
                "auth_required": bool(self.password),
                "logged_in": self._is_authenticated()
            })

        # Проверка авторизации для остальных API эндпоинтов
        if path.startswith("/api/") and not self._is_authenticated():
            return self._send_json({"error": "Требуется авторизация", "auth_required": True}, status=401)

        if path == "/api/info":
            size_bytes = os.path.getsize(self.db.db_path) if os.path.exists(self.db.db_path) else 0
            if size_bytes < 1024:
                size_human = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_human = f"{size_bytes / 1024:.1f} KB"
            else:
                size_human = f"{size_bytes / (1024 * 1024):.2f} MB"

            return self._send_json({
                "db_name": os.path.basename(self.db.db_path),
                "db_path": self.db.db_path,
                "size_bytes": size_bytes,
                "size_human": size_human,
            })

        if path == "/api/tables":
            try:
                tables = self.db.list_tables()
                return self._send_json({"tables": tables})
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        # /api/table/{name}/data
        if path.startswith("/api/table/") and path.endswith("/data"):
            parts = path.split("/")
            if len(parts) == 5:
                table_name = urllib.parse.unquote(parts[3])
                page = int(query.get("page", [1])[0])
                page_size = int(query.get("page_size", [50])[0])
                sort_col = query.get("sort_col", [None])[0]
                sort_dir = query.get("sort_dir", ["ASC"])[0]
                try:
                    res = self.db.get_table_data(table_name, page, page_size, sort_col, sort_dir)
                    return self._send_json(res)
                except Exception as e:
                    return self._send_json({"error": str(e)}, status=400)

        # /api/table/{name}/schema
        if path.startswith("/api/table/") and path.endswith("/schema"):
            parts = path.split("/")
            if len(parts) == 5:
                table_name = urllib.parse.unquote(parts[3])
                try:
                    res = self.db.get_table_schema(table_name)
                    return self._send_json(res)
                except Exception as e:
                    return self._send_json({"error": str(e)}, status=400)

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/login":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
                user_pwd = payload.get("password", "")
                if not self.password or hmac.compare_digest(user_pwd, self.password):
                    token = secrets.token_hex(24)
                    self.sessions.add(token)
                    resp_body = json.dumps({"success": True}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Set-Cookie", f"sqlite_admin_session={token}; HttpOnly; Path=/; SameSite=Lax")
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                    return
                else:
                    return self._send_json({"error": "Неверный пароль"}, status=401)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=400)

        if path == "/api/logout":
            token = self._get_session_token()
            if token and token in self.sessions:
                self.sessions.remove(token)
            resp_body = json.dumps({"success": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", "sqlite_admin_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            return

        # Проверка авторизации для остальных POST запросов
        if path.startswith("/api/") and not self._is_authenticated():
            return self._send_json({"error": "Требуется авторизация", "auth_required": True}, status=401)

        if path == "/api/query":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
                sql = payload.get("sql", "")
                res = self.db.execute_query(sql)
                return self._send_json(res)
            except Exception as e:
                return self._send_json({"error": f"Ошибка обработки запроса: {e}"}, status=400)

        self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        if len(args) > 1 and str(args[1]) in ("400", "500"):
            super().log_message(format, *args)


def find_default_db() -> str:
    for ext in ("*.sqlite", "*.sqlite3", "*.db"):
        found = list(Path(".").glob(ext))
        if found:
            return str(found[0])
    return "database.sqlite"


def run(host: str = "127.0.0.1", port: int = 8080, db_path: str = None, password: str = None):
    if not db_path:
        db_path = find_default_db()

    db_manager = DatabaseManager(db_path)
    AdminHandler.db = db_manager
    AdminHandler.password = password
    AdminHandler.sessions = set()

    server_address = (host, port)
    httpd = http.server.ThreadingHTTPServer(server_address, AdminHandler)
    url = f"http://{host}:{port}"

    print("=" * 60)
    print(f"  SQLite Web Admin запущен!")
    print(f"  Файл базы    : {db_manager.db_path}")
    print(f"  Интерфейс    : {url}")
    print(f"  Авторизация  : {'Включена (по паролю)' if password else 'Отключена'}")
    print("  Нажмите Ctrl+C для остановки")
    print("=" * 60)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SQLite Web Admin")
    parser.add_argument("db", nargs="?", default=None, help="Путь к SQLite файлу базы данных")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Порт (по умолчанию: 8080)")
    parser.add_argument("--host", default="127.0.0.1", help="Хост (по умолчанию: 127.0.0.1)")
    parser.add_argument("--password", "-P", default=os.environ.get("SQLITE_ADMIN_PASSWORD"), help="Пароль для доступа к веб-админке")
    args = parser.parse_args()

    run(host=args.host, port=args.port, db_path=args.db, password=args.password)
