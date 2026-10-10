"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"

_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")  # [3]  [1, 2]  [1-3]; not a Markdown link [3](url)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)        # code blocks / spans: citations there do not count
_REF_LINE = re.compile(r"^\s*\[(\d+)\](.*)$")                     # a reference line: "[n] ..."
_URL = re.compile(r"https?://[^\s<>()\[\]]+")
# RUBRIC 2.2: the family is the tool that returned the source, and the grader checks the url against it
FAMILY_URL = {"arxiv": "https://arxiv.org/abs/", "hf-daily": "https://huggingface.co/papers/",
              "hf-search": "https://huggingface.co/papers/", "web": "http"}
MIN_FAMILIES = 3


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK).

    PSEUDO-CODE:
      problems = []
      if sources is empty: return ["no sources in sources.json"]
      for each source entry:
          n must be an int                       -> problem if not
          url must start with http:// or https://-> problem if not
          the same url must not appear twice     -> problem if duplicated
      split report_text at the heading "## References":
          body = text before it; if the heading is missing -> problem
      cited = set of numbers found as [n] in the BODY only (not in the reference list; use a regex)
      every number in `cited` must exist in sources -> problem "[n] cited but missing from sources.json"
      every source number must be in `cited`        -> problem "source [n] never cited"
      the lines of the References section that start with "[n]" (regex) are the reference lines:
          every source needs exactly ONE reference line (none missing, no number twice, no number that is not a source)
          each reference line holds exactly ONE http(s) URL and it must equal that source's url
          (a line bundling several sources under one number is a problem)
      return problems
    """
    if not sources:
        return ["no sources in sources.json"]
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        return ["sources.json must be a JSON list of objects"]
    problems = []

    # rule 2: every source has an int n, an http(s) url, and no url appears twice
    url_of = {}  # n -> url
    seen_urls = set()
    for s in sources:
        n, url = s.get("n"), s.get("url")
        if not isinstance(n, int) or isinstance(n, bool):
            problems.append(f"source {n!r} ({url}): n must be a JSON integer, e.g. \"n\": 1 (not \"1\")")
        elif n in url_of:
            problems.append(f"source [{n}] appears twice in sources.json")
        else:
            url_of[n] = url
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"source [{n}]: url {url!r} does not start with http:// or https://")
        elif url in seen_urls:
            problems.append(f"source [{n}]: duplicate url {url}")
        else:
            seen_urls.add(url)
        family = s.get("source")
        if family not in FAMILY_URL:
            problems.append(f"source [{n}]: source {family!r} is not one of {sorted(FAMILY_URL)}")
        elif isinstance(url, str) and not url.startswith(FAMILY_URL[family]):
            problems.append(f"source [{n}]: a {family!r} source needs a url starting with {FAMILY_URL[family]}, "
                            f"got {url}")

    families = sorted({s.get("source") for s in sources} & FAMILY_URL.keys())
    if len(families) < MIN_FAMILIES:
        missing = sorted(FAMILY_URL.keys() - set(families))
        problems.append(f"only {len(families)} source families cited ({', '.join(families)}); need >= {MIN_FAMILIES}: "
                        f"cite sources from {' or '.join(missing)} found in the notes, or research them")

    # rule 3: the body is the text before the `## References` heading
    heading = _REF_HEADING.search(report_text)
    if heading is None:
        problems.append("heading '## References' missing from report.md")
        body, references = report_text, ""
    else:
        body, references = report_text[:heading.start()], report_text[heading.end():]

    # rule 4: [n] in the body (grouped forms expanded; code and [n](link) ignored) <-> sources
    cited = set()
    for i, segment in enumerate(_CODE.split(body)):
        if i % 2:  # odd segments are code
            continue
        for match in _GROUP.finditer(segment):
            cited.update(_group_numbers(match.group(1)))
    for n in sorted(cited - url_of.keys()):
        problems.append(f"[{n}] cited but missing from sources.json")
    for n in sorted(url_of.keys() - cited):
        problems.append(f"source [{n}] never cited")

    # rules 5-6: exactly one reference line per source, holding exactly one url = that source's url
    ref_count = {}
    for line in references.splitlines():
        match = _REF_LINE.match(line)
        if not match:
            continue
        n = int(match.group(1))
        ref_count[n] = ref_count.get(n, 0) + 1
        urls = [u.rstrip(".,;>") for u in _URL.findall(match.group(2))]
        if n not in url_of:
            problems.append(f"reference [{n}] is not a source in sources.json")
        elif len(urls) != 1:
            problems.append(f"reference [{n}] must hold exactly one URL, found {len(urls)}")
        elif urls[0] != url_of[n]:
            problems.append(f"reference [{n}] url {urls[0]} differs from sources.json url {url_of[n]}")
    for n, count in sorted(ref_count.items()):
        if count > 1:
            problems.append(f"reference [{n}] appears {count} times in ## References")
    if heading is not None:
        for n in sorted(url_of.keys() - ref_count.keys()):
            problems.append(f"source [{n}] has no line in ## References")
    return problems


def _group_numbers(group):
    """'1, 2' -> [1, 2];  '1-3' -> [1, 2, 3]."""
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            numbers.append(int(part))
    return numbers


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
