"""elstin/tools/outline.py — a file's declarations with line numbers, so a
model can read one method's range instead of paging through the file.

Language-neutral by design (tests/test_architecture.py forbids a tool
branching on a language name), so each language below is exercised only
through the shared heuristics."""
from __future__ import annotations

import os

from elstin.tools.dev_tools import READ_ONLY_TOOLS as DEV_READ_ONLY, TOOL_SCHEMAS, make_dev_tools
from elstin.tools.outline import outline_file, outline_source
from elstin.tools.planner_tools import READ_ONLY_TOOLS as PLANNER_READ_ONLY, make_planner_tools

JAVA = '''package com.example.search;

import java.util.List;

/**
 * Search orders (in the order store).
 */
@Service
public class OrderSearchServiceImpl implements OrderSearchService {

    private static final Logger log = LoggerFactory.getLogger(OrderSearchServiceImpl.class);
    private final OrderRepository repository;

    public OrderSearchServiceImpl(OrderRepository repository) {
        this.repository = repository;
    }

    @Override
    public PageResponse<OrderSearchResult> search(int page, int size, OrderSearchRequest request) {
        log.info("searching {}", request);
        List<Order> orders = repository.find(request);
        orders.forEach(order -> {
            validate(order);
        });
        if (orders.isEmpty()) {
            return PageResponse.empty();
        }
        return PageResponse.of(orders);
    }

    private Sort.Order buildSortOrder(String sort) {
        String sql = """
            WITH matched AS (
              SELECT id FROM orders
            )
            """;
        return Sort.Order.asc(sort);
    }

    public interface Validator {
        boolean validate(Order order);
    }

    public record Page(int number, int size) {}
}
'''

DART = '''import 'package:flutter/material.dart';

class DetailScreen extends StatefulWidget {
  const DetailScreen({super.key});

  factory DetailScreen.fromRoute(RouteSettings settings) => const DetailScreen();

  @override
  State<DetailScreen> createState() => _DetailScreenState();
}

class _DetailScreenState extends State<DetailScreen> {
  @override
  void initState() {
    super.initState();
    setState(() {
      loading = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Text('hello'),
      ),
    );
  }
}
'''

PYTHON = '''"""Module docstring that reads like code: see handle(request) for details.

It mentions process(item) twice (process(item)).
"""
import os


class Handler:
    def handle(self, request):
        print(request)
        return self.process(request)

    async def process(self, item):
        raise ValueError(item)


def main():
    Handler().handle(None)
'''

TS = '''import { useState } from "react";

export interface Props {
  id: string;
}

export function OrderRow({ id }: Props) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    load(id);
  }, [id]);
  return <div>{id}</div>;
}

const load = async (id: string) => {
  await fetch(`/api/${id}`);
};

class Store {
  async refresh(id: string): Promise<void> {
    this.items = await fetchItems(id);
  }
}

defineProps({
  id: String,
});
'''


def _names(source: str) -> list[str]:
    return [line.strip() for _, line in outline_source(source)]


def test_java_lists_types_constructors_and_methods_but_no_statements():
    names = _names(JAVA)

    assert names == [
        "public class OrderSearchServiceImpl implements OrderSearchService {",
        "public OrderSearchServiceImpl(OrderRepository repository) {",
        "public PageResponse<OrderSearchResult> search(int page, int size, OrderSearchRequest request) {",
        "private Sort.Order buildSortOrder(String sort) {",
        "public interface Validator {",
        "boolean validate(Order order);",
        "public record Page(int number, int size) {}",
    ]


def test_line_numbers_point_at_the_declaration():
    entries = dict((line.strip(), number) for number, line in outline_source(JAVA))

    assert JAVA.splitlines()[entries["private Sort.Order buildSortOrder(String sort) {"] - 1].strip().startswith("private Sort.Order")


def test_dart_two_space_indentation_and_named_constructors():
    names = _names(DART)

    assert "class DetailScreen extends StatefulWidget {" in names
    assert "const DetailScreen({super.key});" in names
    assert "factory DetailScreen.fromRoute(RouteSettings settings) => const DetailScreen();" in names
    assert "void initState() {" in names
    assert "Widget build(BuildContext context) {" in names
    assert not any(n.startswith(("setState", "return", "child", "body", "super.")) for n in names)


def test_python_ignores_docstrings_and_statements():
    assert _names(PYTHON) == [
        "class Handler:",
        "def handle(self, request):",
        "async def process(self, item):",
        "def main():",
    ]


def test_typescript_functions_arrows_and_methods_but_not_calls():
    names = _names(TS)

    assert "export interface Props {" in names
    assert "export function OrderRow({ id }: Props) {" in names
    assert "const load = async (id: string) => {" in names
    assert "async refresh(id: string): Promise<void> {" in names
    assert not any(n.startswith(("useEffect", "useState", "defineProps", "const [open")) for n in names)


def _write(root, rel: str, text: str) -> None:
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def test_outline_file_formats_numbered_entries_with_a_header(tmp_path):
    _write(tmp_path, "src/Search.java", JAVA)

    result = outline_file(str(tmp_path), "src/Search.java")
    lines = result.splitlines()

    assert lines[0].startswith("[outline of src/Search.java: 7 declaration(s) in ")
    assert lines[1] == " 9: public class OrderSearchServiceImpl implements OrderSearchService {"


def test_outline_file_caps_the_entries_and_says_so(tmp_path):
    _write(tmp_path, "many.py", "".join(f"def f{i}():\n    pass\n" for i in range(30)))

    result = outline_file(str(tmp_path), "many.py", max_entries=10)

    assert "... (20 more not shown)" in result


def test_outline_file_with_nothing_to_list_says_so(tmp_path):
    _write(tmp_path, "notes.txt", "just some text\n")

    assert "no declarations recognized" in outline_file(str(tmp_path), "notes.txt")


def test_both_roles_offer_it_as_a_read_only_tool(tmp_path):
    _write(tmp_path, "src/Search.java", JAVA)
    dev_functions, _ = make_dev_tools(
        worktree_path=str(tmp_path), allowed_paths=set(), analyze_targets=[], allow_test_run=False, planned_test_files=[],
    )
    planner_functions, planner_schemas = make_planner_tools(project_path=str(tmp_path))

    assert "public class OrderSearchServiceImpl" in dev_functions["outline_file_tool"]("src/Search.java")
    assert "public class OrderSearchServiceImpl" in planner_functions["outline_file_tool"]("src/Search.java")
    assert dev_functions["outline_file_tool"]("missing.java") == "File not found: missing.java"
    assert "outline_file_tool" in DEV_READ_ONLY and "outline_file_tool" in PLANNER_READ_ONLY
    assert any(s["function"]["name"] == "outline_file_tool" for s in TOOL_SCHEMAS)
    assert any(s["function"]["name"] == "outline_file_tool" for s in planner_schemas)


def test_it_cannot_read_outside_the_project(tmp_path):
    functions, _ = make_planner_tools(project_path=str(tmp_path))

    assert functions["outline_file_tool"]("../../etc/passwd").startswith(("BLOCKED", "File not found"))
