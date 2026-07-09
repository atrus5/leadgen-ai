#!/bin/bash
# Runs the LeadGen AI smoke tests. Sets PYTHONPATH so `ai_agency`
# resolves as a top-level package from the test runner's cwd.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PACKAGE_PARENT="$(cd "$PROJECT_ROOT/.." && pwd)"

cd "$PACKAGE_PARENT"
PYTHONPATH="$PACKAGE_PARENT" python -m ai_agency.tests.test_smoke
