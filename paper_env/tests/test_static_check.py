from paper_env.kernel.static import analyze_cell


def test_flags_undeclared_reads_but_not_locals_imports_or_builtins():
    src = "import numpy as np\ny = x + len([1])\ndef f(a):\n    b = a + z\n    return b\nprint(f(y))"
    w = analyze_cell(src, declared_inputs=["z"])
    assert w == ["reads 'x' (line 2) which is not declared as an input"]


def test_declared_inputs_are_fine():
    assert analyze_cell("y = x * 2", ["x"]) == []


def test_comprehension_loop_and_except_names_are_defined():
    src = "total = sum(i for i in range(3))\nfor k in range(2):\n    pass\ntry:\n    pass\nexcept Exception as e:\n    print(e)"
    assert analyze_cell(src, []) == []


def test_magics_do_not_break_parsing_and_pip_is_flagged():
    w = analyze_cell("%matplotlib inline\n!pip install foo\nx = 1", [])
    assert w == ["installs packages inside a cell; use install_package()/pin_version() instead"]


def test_subprocess_pip_install_flagged():
    w = analyze_cell("import subprocess\nsubprocess.run(['pip', 'install', 'foo'])", [])
    assert any("installs packages" in x for x in w)
    w = analyze_cell("import os\nos.system('pip install foo')", [])
    assert any("installs packages" in x for x in w)


def test_syntax_error_is_reported_not_raised():
    assert analyze_cell("def (:", []) == ["could not analyze: syntax error at line 1"]


def test_never_executes_code():
    # if this were evaluated it would raise/exit; analysis must be purely static
    assert analyze_cell("import os\nos._exit(3)\n__import__('sys').exit(2)", []) == []
