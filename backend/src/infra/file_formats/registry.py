"""File format registry — backend single source of truth.

Mirrors `frontend/lib/fileFormats/registry.ts`. The two files are
plain-data; adding a format means editing both. Backend exposes
three pure functions (``detect_mime`` / ``detect_node_type`` /
``detect_ingest_type``); the underlying lookup tables are built
once at module import.

Resolution rule:
  1. Exact basename match (case-insensitive) — handles
     ``Makefile``, ``Dockerfile``, ``README``, etc.
  2. Extension match (longest suffix wins so ``.tar.gz`` beats
     ``.gz``).
  3. ``application/octet-stream`` fallback for MIME, ``"file"`` for
     node type, ``DOCUMENT`` for ingest type.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from src.infra.task_presentation import IngestType


@dataclass(frozen=True)
class FormatSpec:
    """One file format. Extensions include their leading dot.

    `node_type` mirrors the legacy tree node taxonomy (``"json"``,
    ``"markdown"``, ``"file"``). ``"folder"`` is set by the tree
    walker, never resolved from a filename.
    """
    extensions: tuple[str, ...]
    mime: str
    node_type: str
    ingest: IngestType
    filenames: tuple[str, ...] = field(default_factory=tuple)


# Order matters only for documentation — the lookup is a flat dict.
FILE_FORMATS: list[FormatSpec] = [
    # ── Markdown ────────────────────────────────────────────────────
    FormatSpec(('.md', '.markdown', '.mkd', '.mdown'), 'text/markdown', 'markdown', IngestType.TEXT),
    FormatSpec(('.mdx',), 'text/markdown', 'file', IngestType.TEXT),

    # ── Structured data ─────────────────────────────────────────────
    FormatSpec(('.json',), 'application/json', 'json', IngestType.TEXT),
    FormatSpec(('.json5', '.jsonc'), 'application/json', 'file', IngestType.TEXT),
    FormatSpec(('.jsonl', '.ndjson'), 'application/x-ndjson', 'file', IngestType.TEXT),
    FormatSpec(('.yaml', '.yml'), 'application/yaml', 'file', IngestType.TEXT),
    FormatSpec(('.toml',), 'application/toml', 'file', IngestType.TEXT),
    FormatSpec(('.xml', '.plist', '.rss', '.atom', '.xsl', '.xsd'), 'application/xml', 'file', IngestType.TEXT),
    FormatSpec(('.csv',), 'text/csv', 'file', IngestType.TEXT),
    FormatSpec(('.tsv',), 'text/tab-separated-values', 'file', IngestType.TEXT),
    FormatSpec(('.proto',), 'text/x-protobuf', 'file', IngestType.TEXT),
    FormatSpec(
        ('.ini', '.cfg', '.conf', '.config', '.editorconfig', '.gitconfig'),
        'text/x-ini', 'file', IngestType.TEXT,
    ),
    FormatSpec(('.properties',), 'text/x-properties', 'file', IngestType.TEXT),
    FormatSpec(('.hcl', '.tf', '.tfvars'), 'text/x-hcl', 'file', IngestType.TEXT),

    # ── Code: TypeScript / JavaScript / Web ─────────────────────────
    FormatSpec(('.ts', '.tsx', '.mts', '.cts'), 'application/typescript', 'file', IngestType.TEXT),
    FormatSpec(('.js', '.jsx', '.mjs', '.cjs'), 'application/javascript', 'file', IngestType.TEXT),
    FormatSpec(('.coffee',), 'application/vnd.coffeescript', 'file', IngestType.TEXT),
    FormatSpec(('.html', '.htm', '.xhtml'), 'text/html', 'file', IngestType.TEXT),
    FormatSpec(('.css',), 'text/css', 'file', IngestType.TEXT),
    FormatSpec(('.scss',), 'text/x-scss', 'file', IngestType.TEXT),
    FormatSpec(('.sass',), 'text/x-sass', 'file', IngestType.TEXT),
    FormatSpec(('.less',), 'text/x-less', 'file', IngestType.TEXT),
    FormatSpec(('.pug', '.jade'), 'text/x-pug', 'file', IngestType.TEXT),
    FormatSpec(('.hbs', '.handlebars', '.mustache'), 'text/x-handlebars-template', 'file', IngestType.TEXT),
    FormatSpec(('.cshtml', '.razor'), 'text/x-cshtml', 'file', IngestType.TEXT),
    FormatSpec(('.twig',), 'text/x-twig', 'file', IngestType.TEXT),
    FormatSpec(('.liquid',), 'text/x-liquid', 'file', IngestType.TEXT),

    # ── Code: Backend / Systems ─────────────────────────────────────
    FormatSpec(('.py', '.pyw', '.pyi', '.pyx'), 'text/x-python', 'file', IngestType.TEXT),
    FormatSpec(
        ('.rb', '.rake', '.gemspec', '.ru'),
        'text/x-ruby', 'file', IngestType.TEXT,
        filenames=(
            'Rakefile', 'Gemfile', 'Capfile', 'Berksfile', 'Brewfile',
            'Cheffile', 'Podfile', 'Fastfile', 'Appfile', 'Deliverfile',
            'Snapfile', 'Matchfile', 'Vagrantfile', 'Guardfile', 'Procfile',
        ),
    ),
    FormatSpec(('.rs',), 'text/x-rust', 'file', IngestType.TEXT),
    FormatSpec(('.go',), 'text/x-go', 'file', IngestType.TEXT),
    FormatSpec(('.java',), 'text/x-java-source', 'file', IngestType.TEXT),
    FormatSpec(('.kt', '.kts'), 'text/x-kotlin', 'file', IngestType.TEXT),
    FormatSpec(('.swift',), 'text/x-swift', 'file', IngestType.TEXT),
    FormatSpec(('.m', '.mm'), 'text/x-objectivec', 'file', IngestType.TEXT),
    FormatSpec(('.c', '.h'), 'text/x-c', 'file', IngestType.TEXT),
    FormatSpec(
        ('.cpp', '.cc', '.cxx', '.hpp', '.hh', '.hxx', '.ipp', '.tpp'),
        'text/x-c++', 'file', IngestType.TEXT,
    ),
    FormatSpec(('.cs', '.csx'), 'text/x-csharp', 'file', IngestType.TEXT),
    FormatSpec(('.fs', '.fsi', '.fsx', '.fsscript'), 'text/x-fsharp', 'file', IngestType.TEXT),
    FormatSpec(('.vb', '.vbs'), 'text/x-vb', 'file', IngestType.TEXT),
    FormatSpec(('.pas', '.pp', '.dpr'), 'text/x-pascal', 'file', IngestType.TEXT),
    FormatSpec(('.hs', '.lhs'), 'text/x-haskell', 'file', IngestType.TEXT),
    FormatSpec(('.erl', '.hrl'), 'text/x-erlang', 'file', IngestType.TEXT),
    FormatSpec(('.ex', '.exs', '.eex', '.heex', '.leex'), 'text/x-elixir', 'file', IngestType.TEXT),
    FormatSpec(('.clj', '.cljs', '.cljc', '.edn'), 'text/x-clojure', 'file', IngestType.TEXT),
    FormatSpec(('.scala', '.sc'), 'text/x-scala', 'file', IngestType.TEXT),
    FormatSpec(('.dart',), 'application/dart', 'file', IngestType.TEXT),
    FormatSpec(('.jl',), 'text/x-julia', 'file', IngestType.TEXT),
    FormatSpec(('.r', '.rmd'), 'text/x-r', 'file', IngestType.TEXT),
    FormatSpec(('.lua',), 'text/x-lua', 'file', IngestType.TEXT),
    FormatSpec(('.pl', '.pm', '.t', '.pod'), 'text/x-perl', 'file', IngestType.TEXT),
    FormatSpec(('.php', '.phtml', '.phar'), 'application/x-php', 'file', IngestType.TEXT),
    FormatSpec(
        ('.sh', '.bash', '.zsh', '.fish', '.ash', '.ksh'),
        'application/x-sh', 'file', IngestType.TEXT,
    ),
    FormatSpec(('.ps1', '.psm1', '.psd1'), 'application/x-powershell', 'file', IngestType.TEXT),
    FormatSpec(('.bat', '.cmd'), 'application/x-bat', 'file', IngestType.TEXT),
    FormatSpec(('.tcl',), 'application/x-tcl', 'file', IngestType.TEXT),
    FormatSpec(('.sql', '.psql', '.mysql'), 'application/sql', 'file', IngestType.TEXT),
    FormatSpec(('.graphql', '.gql'), 'application/graphql', 'file', IngestType.TEXT),
    FormatSpec(('.sol',), 'text/x-solidity', 'file', IngestType.TEXT),
    FormatSpec(('.v', '.sv', '.vh', '.svh'), 'text/x-systemverilog', 'file', IngestType.TEXT),

    # ── Build / infra files ─────────────────────────────────────────
    FormatSpec(
        ('.dockerfile',),
        'text/x-dockerfile', 'file', IngestType.TEXT,
        filenames=('Dockerfile', 'Containerfile'),
    ),
    FormatSpec(
        ('.mk', '.mak'),
        'text/x-makefile', 'file', IngestType.TEXT,
        filenames=('Makefile', 'GNUmakefile', 'makefile'),
    ),
    FormatSpec(
        ('.cmake',),
        'text/x-cmake', 'file', IngestType.TEXT,
        filenames=('CMakeLists.txt',),
    ),
    FormatSpec(
        ('.tex', '.latex', '.ltx', '.sty', '.cls', '.bib'),
        'application/x-tex', 'file', IngestType.TEXT,
    ),
    FormatSpec(('.rst',), 'text/x-rst', 'file', IngestType.TEXT),
    FormatSpec(('.diff', '.patch'), 'text/x-diff', 'file', IngestType.TEXT),

    # ── Plain text + extensionless metadata ─────────────────────────
    FormatSpec(
        ('.txt', '.text', '.log', '.env'),
        'text/plain', 'file', IngestType.TEXT,
        filenames=(
            'README', 'LICENSE', 'COPYING', 'INSTALL', 'AUTHORS',
            'CONTRIBUTORS', 'NOTICE', 'CHANGELOG', 'CHANGES', 'HISTORY',
            'TODO', 'NEWS', 'CREDITS', 'MAINTAINERS', 'VERSION',
        ),
    ),

    # ── Subtitles ───────────────────────────────────────────────────
    FormatSpec(('.srt',), 'application/x-subrip', 'file', IngestType.TEXT),
    FormatSpec(('.vtt',), 'text/vtt', 'file', IngestType.TEXT),
    FormatSpec(('.ass', '.ssa'), 'text/x-ass', 'file', IngestType.TEXT),

    # ── Images ──────────────────────────────────────────────────────
    FormatSpec(('.png', '.apng'), 'image/png', 'file', IngestType.IMAGE),
    FormatSpec(('.jpg', '.jpeg', '.jpe', '.jfif', '.pjpeg', '.pjp'), 'image/jpeg', 'file', IngestType.IMAGE),
    FormatSpec(('.gif',), 'image/gif', 'file', IngestType.IMAGE),
    FormatSpec(('.webp',), 'image/webp', 'file', IngestType.IMAGE),
    FormatSpec(('.avif',), 'image/avif', 'file', IngestType.IMAGE),
    FormatSpec(('.svg',), 'image/svg+xml', 'file', IngestType.IMAGE),
    FormatSpec(('.bmp',), 'image/bmp', 'file', IngestType.IMAGE),
    FormatSpec(('.ico',), 'image/x-icon', 'file', IngestType.IMAGE),
    FormatSpec(('.tif', '.tiff'), 'image/tiff', 'file', IngestType.IMAGE),
    FormatSpec(('.heic',), 'image/heic', 'file', IngestType.IMAGE),
    FormatSpec(('.heif',), 'image/heif', 'file', IngestType.IMAGE),
    FormatSpec(
        ('.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2'),
        'image/x-raw', 'file', IngestType.IMAGE,
    ),
    FormatSpec(('.psd',), 'image/vnd.adobe.photoshop', 'file', IngestType.IMAGE),

    # ── Audio ───────────────────────────────────────────────────────
    FormatSpec(('.mp3',), 'audio/mpeg', 'file', IngestType.DOCUMENT),
    FormatSpec(('.wav', '.wave'), 'audio/wav', 'file', IngestType.DOCUMENT),
    FormatSpec(('.ogg', '.oga'), 'audio/ogg', 'file', IngestType.DOCUMENT),
    FormatSpec(('.opus',), 'audio/opus', 'file', IngestType.DOCUMENT),
    FormatSpec(('.m4a', '.m4b'), 'audio/mp4', 'file', IngestType.DOCUMENT),
    FormatSpec(('.flac',), 'audio/flac', 'file', IngestType.DOCUMENT),
    FormatSpec(('.aac',), 'audio/aac', 'file', IngestType.DOCUMENT),
    FormatSpec(('.weba',), 'audio/webm', 'file', IngestType.DOCUMENT),
    FormatSpec(('.aiff', '.aif', '.aifc'), 'audio/aiff', 'file', IngestType.DOCUMENT),
    FormatSpec(('.wma',), 'audio/x-ms-wma', 'file', IngestType.DOCUMENT),
    FormatSpec(('.mid', '.midi'), 'audio/midi', 'file', IngestType.DOCUMENT),

    # ── Video ───────────────────────────────────────────────────────
    FormatSpec(('.mp4', '.m4v'), 'video/mp4', 'file', IngestType.DOCUMENT),
    FormatSpec(('.webm',), 'video/webm', 'file', IngestType.DOCUMENT),
    FormatSpec(('.ogv',), 'video/ogg', 'file', IngestType.DOCUMENT),
    FormatSpec(('.mov', '.qt'), 'video/quicktime', 'file', IngestType.DOCUMENT),
    FormatSpec(('.avi',), 'video/x-msvideo', 'file', IngestType.DOCUMENT),
    FormatSpec(('.mkv',), 'video/x-matroska', 'file', IngestType.DOCUMENT),
    FormatSpec(('.wmv',), 'video/x-ms-wmv', 'file', IngestType.DOCUMENT),
    FormatSpec(('.flv',), 'video/x-flv', 'file', IngestType.DOCUMENT),
    FormatSpec(('.mpg', '.mpeg', '.mpe', '.m2v'), 'video/mpeg', 'file', IngestType.DOCUMENT),
    FormatSpec(('.3gp', '.3gpp', '.3g2'), 'video/3gpp', 'file', IngestType.DOCUMENT),

    # ── Documents ───────────────────────────────────────────────────
    FormatSpec(('.pdf',), 'application/pdf', 'file', IngestType.PDF),
    FormatSpec(
        ('.docx',),
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'file', IngestType.DOCUMENT,
    ),
    FormatSpec(('.doc',), 'application/msword', 'file', IngestType.DOCUMENT),
    FormatSpec(
        ('.xlsx', '.xlsm', '.xlsb'),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'file', IngestType.DOCUMENT,
    ),
    FormatSpec(('.xls',), 'application/vnd.ms-excel', 'file', IngestType.DOCUMENT),
    FormatSpec(
        ('.pptx', '.pps', '.ppsx'),
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'file', IngestType.DOCUMENT,
    ),
    FormatSpec(('.ppt',), 'application/vnd.ms-powerpoint', 'file', IngestType.DOCUMENT),
    # OpenDocument
    FormatSpec(('.odt', '.ott'), 'application/vnd.oasis.opendocument.text', 'file', IngestType.DOCUMENT),
    FormatSpec(('.ods', '.ots'), 'application/vnd.oasis.opendocument.spreadsheet', 'file', IngestType.DOCUMENT),
    FormatSpec(('.odp', '.otp'), 'application/vnd.oasis.opendocument.presentation', 'file', IngestType.DOCUMENT),
    # Apple iWork
    FormatSpec(('.pages',), 'application/x-iwork-pages-sffpages', 'file', IngestType.DOCUMENT),
    FormatSpec(('.numbers',), 'application/x-iwork-numbers-sffnumbers', 'file', IngestType.DOCUMENT),
    # `.key` is intentionally absent — it conflicts with SSL private
    # keys, which dominate developer contexts. Keynote files surface
    # only via MIME fallback if the server detects it.
    FormatSpec((), 'application/x-iwork-keynote-sffkey', 'file', IngestType.DOCUMENT),
    # Rich text / e-books
    FormatSpec(('.rtf',), 'application/rtf', 'file', IngestType.DOCUMENT),
    FormatSpec(('.epub',), 'application/epub+zip', 'file', IngestType.DOCUMENT),
    FormatSpec(('.mobi', '.azw', '.azw3'), 'application/x-mobipocket-ebook', 'file', IngestType.DOCUMENT),
    # Jupyter
    FormatSpec(('.ipynb',), 'application/x-ipynb+json', 'file', IngestType.TEXT),

    # ── Archives ────────────────────────────────────────────────────
    FormatSpec(('.zip',), 'application/zip', 'file', IngestType.DOCUMENT),
    FormatSpec(
        ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz', '.tbz2', '.tar.xz', '.txz'),
        'application/x-tar', 'file', IngestType.DOCUMENT,
    ),
    FormatSpec(('.gz',), 'application/gzip', 'file', IngestType.DOCUMENT),
    FormatSpec(('.bz2', '.bz'), 'application/x-bzip2', 'file', IngestType.DOCUMENT),
    FormatSpec(('.xz', '.lzma'), 'application/x-xz', 'file', IngestType.DOCUMENT),
    FormatSpec(('.7z',), 'application/x-7z-compressed', 'file', IngestType.DOCUMENT),
    FormatSpec(('.rar',), 'application/vnd.rar', 'file', IngestType.DOCUMENT),
    FormatSpec(('.dmg',), 'application/x-apple-diskimage', 'file', IngestType.DOCUMENT),
    FormatSpec(('.iso', '.img'), 'application/x-iso9660-image', 'file', IngestType.DOCUMENT),

    # ── Fonts ───────────────────────────────────────────────────────
    FormatSpec(('.ttf', '.ttc'), 'font/ttf', 'file', IngestType.DOCUMENT),
    FormatSpec(('.otf',), 'font/otf', 'file', IngestType.DOCUMENT),
    FormatSpec(('.woff',), 'font/woff', 'file', IngestType.DOCUMENT),
    FormatSpec(('.woff2',), 'font/woff2', 'file', IngestType.DOCUMENT),
    FormatSpec(('.eot',), 'application/vnd.ms-fontobject', 'file', IngestType.DOCUMENT),

    # ── Database / 3D / Misc binary ─────────────────────────────────
    FormatSpec(('.db', '.sqlite', '.sqlite3', '.db3'), 'application/vnd.sqlite3', 'file', IngestType.DOCUMENT),
    FormatSpec(('.obj', '.stl', '.gltf', '.glb', '.fbx', '.dae', '.ply'), 'model/obj', 'file', IngestType.DOCUMENT),
    FormatSpec(
        ('.pem', '.crt', '.cer', '.der', '.key', '.pub', '.csr', '.p12', '.pfx'),
        'application/x-pem-file', 'file', IngestType.DOCUMENT,
    ),
    FormatSpec(
        ('.exe', '.dll', '.so', '.dylib', '.app', '.msi', '.deb', '.rpm', '.pkg', '.apk', '.ipa'),
        'application/x-msdownload', 'file', IngestType.DOCUMENT,
    ),
]


# Pre-built flat lookup tables — built once at import.
_BY_FILENAME: dict[str, FormatSpec] = {
    fname.lower(): fmt for fmt in FILE_FORMATS for fname in fmt.filenames
}
_BY_EXT: dict[str, FormatSpec] = {
    ext: fmt for fmt in FILE_FORMATS for ext in fmt.extensions
}
_EXTS_LONGEST_FIRST = tuple(
    sorted(_BY_EXT.keys(), key=len, reverse=True)
)


def _match_extension(name: str) -> FormatSpec | None:
    """Return the format for the longest matching filename suffix.

    ``os.path.splitext`` returns ``''`` for dotfiles (``.env``,
    ``.gitignore``), and it can't distinguish compound extensions
    like ``.tar.gz``. Matching against the pre-sorted suffix table
    keeps backend behaviour aligned with the frontend resolver.
    """
    base = os.path.basename(name).lower()
    return next(
        (_BY_EXT[ext] for ext in _EXTS_LONGEST_FIRST if base.endswith(ext)),
        None,
    )


def _resolve(name: str) -> FormatSpec | None:
    """Filename-first → extension fallback. None = unknown."""
    base = os.path.basename(name).lower()
    by_name = _BY_FILENAME.get(base)
    if by_name:
        return by_name
    return _match_extension(name)


def detect_mime(name: str) -> str:
    """Filename → MIME string. Falls back to ``application/octet-stream``."""
    fmt = _resolve(name)
    return fmt.mime if fmt else "application/octet-stream"


def detect_node_type(name: str) -> str:
    """Filename → tree node taxonomy: ``"json" | "markdown" | "file"``.

    ``"folder"`` is set by tree walkers, never resolved from a filename.
    """
    fmt = _resolve(name)
    return fmt.node_type if fmt else "file"


def detect_ingest_type(name: str) -> IngestType:
    """Filename → ingest pipeline bucket.

    Unknown extensions go to ``DOCUMENT`` (the catch-all bucket for
    binary content the OCR/parse pipeline can attempt to decode).
    """
    fmt = _resolve(name)
    return fmt.ingest if fmt else IngestType.DOCUMENT
