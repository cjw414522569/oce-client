"""分层忽略规则：硬规则 → 运行时 → .oceignore → .gitignore → 内置默认。

.gitignore 语义
---------------
根目录与**各子目录**的 .gitignore 都会被读取，按 git 的规则评估：某个
.gitignore 只管辖自己所在目录及其子树，其中模式相对该目录解释。因此
`sub/.gitignore` 里的 `generated/` 命中 `sub/generated/` 与
`sub/deep/generated/`，但不影响 `other/generated/`；左右斜杠开头的
`/anchored.py` 只命中该目录本身。

内置默认
--------
项目没有 .gitignore（或没覆盖到）时，仍排除各语言生态的构建产物与缓存，
避免 node_modules、target、__pycache__ 之类噪音进入索引。默认层优先级
最低，可在 .gitignore/.oceignore 里用 `!pattern` 反选回来；`.git/` 与
`.oce-client/` 属硬规则，任何层都无法反选。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from pathspec.gitignore import GitIgnoreSpec


# 各语言/工具链的构建产物与缓存目录（无前导斜杠 = 匹配任意层级，同 git）。
# 只收录「几乎不可能是源码」的目录名；刻意排除 bin/ lib/ out/ env/ packages/
# 这类语义冲突的通用名（它们是 C++/Go/Python/npm monorepo 的正常源码目录），
# 也排除 vendor/（Go/PHP 的第三方依赖，但被提交进仓库时属于项目内容）。
DEFAULT_PATTERNS: tuple[str, ...] = (
    # 通用
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".cache/",
    ".sandbox/",
    # Python
    ".venv/",
    "venv/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    ".ipynb_checkpoints/",
    ".hypothesis/",
    # Node / 前端
    "node_modules/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    ".astro/",
    ".turbo/",
    ".parcel-cache/",
    ".vite/",
    ".yarn/",
    ".pnpm-store/",
    "bower_components/",
    # 通用产物目录
    "dist/",
    "build/",
    "coverage/",
    # Rust / Go / JVM / Scala
    "target/",
    ".gradle/",
    ".mvn/",
    ".bloop/",
    ".metals/",
    # CMake / C / C++
    "cmake-build-debug/",
    "cmake-build-release/",
    "CMakeFiles/",
    # C# / .NET（packages/ 刻意不收：它是 npm/Python monorepo 的常见源码目录）
    "obj/",
    # Apple / 移动端
    "Pods/",
    "DerivedData/",
    ".dart_tool/",
    ".pub-cache/",
    "flutter/ephemeral/",
    # Elixir / Erlang / OCaml / Haskell / Swift
    "_build/",
    "deps/",
    ".stack-work/",
    "dist-newstyle/",
    ".build/",
    # 基础设施 / 编辑器 / 工具
    ".terraform/",
    ".terragrunt-cache/",
    ".idea/",
    ".vscode/",
    ".history/",
    ".ccls-cache/",
    ".clangd/",
)

# 编译产物与二进制后缀，按文件名匹配（与目录规则分开便于核对）。
DEFAULT_FILE_PATTERNS: tuple[str, ...] = (
    "*.pyc",
    "*.pyo",
    "*.class",
    "*.o",
    "*.obj",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.a",
    "*.lib",
    "*.exe",
    "*.wasm",
    "*.min.js",
    "*.min.css",
    "*.map",
)

# 硬规则：任何层（含 .gitignore 的 !negation）都无法反选。
_HARD_PATTERNS: tuple[str, ...] = (".git/", ".git/**", ".oce-client/", ".oce-client/**")

# 扫描 .gitignore 时不下探的目录：工具/依赖目录，其规则文件对本项目无意义，
# 且体量巨大（node_modules 里可能上万份 .gitignore）。
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".oce-client",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        ".nox",
        ".next",
        ".nuxt",
        "target",
        "Pods",
        "DerivedData",
        ".dart_tool",
        "_build",
        "dist-newstyle",
    }
)
# 限制 .gitignore 扫描深度：正常源码树远小于此，超出即视为产物目录
_MAX_SCAN_DEPTH = 12


class _RuleLayer:
    def __init__(self, lines: Iterable[str]) -> None:
        cleaned = []
        for raw in lines:
            line = raw.strip("\r\n")
            if line and not line.startswith("#"):
                cleaned.append(line)
        self.patterns = list(GitIgnoreSpec.from_lines(cleaned).patterns)
        self.empty = not self.patterns

    def match(self, path: str, is_dir: bool) -> bool | None:
        if self.empty:
            return None
        candidate = path.rstrip("/") + ("/" if is_dir else "")
        result: bool | None = None
        for pattern in self.patterns:
            if pattern.match_file(candidate) is not None:
                # GitWildMatchPattern.include=True means the path is ignored;
                # a leading ! produces include=False and re-includes it.
                result = bool(pattern.include)
        return result


class _ScopedRules:
    """某目录下的 .gitignore：模式相对该目录解释，只管辖该目录子树。"""

    __slots__ = ("base", "depth", "layer")

    def __init__(self, base: str, layer: _RuleLayer) -> None:
        self.base = base
        # 目录深度（根=0，sub=1，sub/deeper=2）：用于深层优先排序。
        # 注意不能用 base.count("/")——"sub" 与 "" 会得到同一个键。
        self.depth = len(base.split("/")) if base else 0
        self.layer = layer

    def match(self, path: str, is_dir: bool) -> bool | None:
        if self.base:
            prefix = self.base + "/"
            if not path.startswith(prefix):
                return None
            return self.layer.match(path[len(prefix) :], is_dir)
        return self.layer.match(path, is_dir)


class LayeredIgnoreMatcher:
    """Merge runtime, project, per-directory git, and built-in ignore rules.

    优先级（高 → 低）：硬规则 > 运行时(--ignore) > .oceignore > .gitignore
    （目录越深越优先）> 内置默认。首个给出结论的层即生效，因此高层可以用
    反选改写低层判断。
    """

    def __init__(
        self,
        root: Path,
        runtime_patterns: Iterable[str] = (),
        *,
        oceignore_name: str = ".oceignore",
        gitignore_name: str = ".gitignore",
        scan_ignore_files: bool = True,
    ) -> None:
        self.root = root
        self._hard = _RuleLayer(_HARD_PATTERNS)
        self._runtime = _RuleLayer(runtime_patterns)
        self._oce = _RuleLayer(self._read_lines(root / oceignore_name))
        self._git: list[_ScopedRules] = [
            _ScopedRules("", _RuleLayer(self._read_lines(root / gitignore_name)))
        ]
        if scan_ignore_files:
            self._git.extend(self._scan_gitignores(root, gitignore_name))
        # 深层规则优先于浅层：git 的逐目录 .gitignore 语义要求整表按深度降序，
        # 根层若排在前面会先给结论，深层的反选就永远看不到。
        self._git.sort(key=lambda item: item.depth, reverse=True)
        self._defaults = _RuleLayer(DEFAULT_PATTERNS + DEFAULT_FILE_PATTERNS)

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, UnicodeDecodeError, OSError):
            return []

    @classmethod
    def _scan_gitignores(cls, root: Path, gitignore_name: str) -> list[_ScopedRules]:
        """收集各子目录的 .gitignore（根目录那份由 __init__ 负责）。

        用裸字符串管理深度与相对路径：os.walk 已给出目录串，逐目录
        Path.relative_to 在十万级目录的仓库上是主要开销。
        """
        found: list[_ScopedRules] = []
        root_str = os.path.abspath(os.fspath(root))
        root_depth = root_str.rstrip(os.sep).count(os.sep)
        for directory, dirnames, filenames in os.walk(root_str, followlinks=False):
            depth = directory.rstrip(os.sep).count(os.sep) - root_depth
            if depth >= _MAX_SCAN_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
            if directory != root_str and gitignore_name in filenames:
                rule = _RuleLayer(cls._read_lines(Path(directory) / gitignore_name))
                if not rule.empty:
                    base = os.path.relpath(directory, root_str).replace(os.sep, "/")
                    found.append(_ScopedRules(base, rule))
        return found

    def ignores(self, path: str, *, is_dir: bool = False) -> bool:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        # A hard rule cannot be undone by a higher-priority negation.
        if self._hard.match(normalized, is_dir) is True:
            return True
        for layer in (self._runtime, self._oce):
            decision = layer.match(normalized, is_dir)
            if decision is not None:
                return decision
        # 深层 .gitignore 优先于浅层：列表已在构造时按 depth 降序排好
        for scoped in self._git:
            decision = scoped.match(normalized, is_dir)
            if decision is not None:
                return decision
        return bool(self._defaults.match(normalized, is_dir))
