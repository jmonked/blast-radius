"""Build-time: download sqlglot at the pinned commit into /opt/sqlglot."""

import io
import shutil
import sys
import tarfile
import urllib.request

commit = sys.argv[1]
dest = sys.argv[2]
url = f"https://github.com/tobymao/sqlglot/archive/{commit}.tar.gz"
for attempt in range(3):
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
        break
    except Exception as error:  # retry transient network errors
        print(f"download failed ({error!r}), retrying", file=sys.stderr)
else:
    sys.exit("could not download sqlglot")
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/tmp/sqlglot-src", filter="data")
shutil.move(f"/tmp/sqlglot-src/sqlglot-{commit}", dest)
print(f"sqlglot {commit} -> {dest}", file=sys.stderr)
