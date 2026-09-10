"""Show staged/unstaged Git diffs and untracked filenames in a review terminal."""

from pathlib import Path
import subprocess
import sys


def review(root):
    sections = []
    for title, args in [
        ("Status (includes untracked files)", ["status", "--short"]),
        ("Staged changes", ["diff", "--cached", "--no-ext-diff", "--no-textconv"]),
        ("Unstaged changes", ["diff", "--no-ext-diff", "--no-textconv"]),
    ]:
        result = subprocess.run(["git", "-C", str(root), "-c", "color.ui=always", *args],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        body = result.stdout or "(none)\n"
        sections.append(f"\n{title}\n{'=' * len(title)}\n{body}")
    return f"Changes in {root}\n" + "\n".join(sections)


if __name__ == "__main__":
    try:
        text = review(Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve())
        if sys.stdout.isatty():
            subprocess.run(["less", "-R"], input=text, text=True, check=True)
        else:
            print(text)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from exc
