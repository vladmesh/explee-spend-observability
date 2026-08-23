# Vendored browser dependencies

The dashboard is a public read-only artifact, so it does not load code from a third-party host at
render time. A CDN reference would make the page depend on that host being reachable and would
execute whatever it returns, since a script tag without an integrity hash is trusted blindly.
Serving the file from `/static` also allows a strict content security policy and keeps the viewer's
address out of a third party's logs.

## Contents

| file | version | license | sha256 |
| --- | --- | --- | --- |
| `echarts-5.6.0.min.js` | 5.6.0 | Apache-2.0 (notice retained in the file header) | `bf4a223524e40b77c304bec67e1222cf551f14880cf42c69dc046558e11c07b1` |

The version is part of the filename so the file can be cached indefinitely: a new version is a new
URL rather than a stale copy in someone's browser.

## Refreshing

```bash
curl -fsSL -o src/explee_test/web/vendor/echarts-<version>.min.js \
  https://cdn.jsdelivr.net/npm/echarts@<version>/dist/echarts.min.js
sha256sum src/explee_test/web/vendor/echarts-<version>.min.js
```

Update the table above, the `<script>` tag in `dashboard.html`, and delete the previous file in the
same commit, so the change is one reviewable diff.

Only line, bar, and scatter charts are used, with tooltip, legend, grid, dataZoom, and markLine. A
tree-shaken build would be roughly a third of the size, but it would add a JavaScript toolchain to a
repository that currently has none; that trade is not worth 700 KB on a dashboard that is opened
occasionally and served compressed.
