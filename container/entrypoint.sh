#!/bin/sh
# Container entrypoint: decide Hugging Face offline mode before Python starts
# (huggingface_hub latches HF_HUB_OFFLINE at import time, so it cannot be set
# from inside the app).
#
# When HF_HUB_OFFLINE is unset/empty, probe huggingface.co: if it is
# unreachable, enable offline mode so model loads use only the local cache
# ($HF_HOME, persisted in the data volume) and fail fast instead of hanging
# on connection timeouts. Set HF_HUB_OFFLINE=1 (or 0) to skip the probe and
# force a mode. Best-effort: the server is started either way.

if [ -z "${HF_HUB_OFFLINE:-}" ]; then
    if ! curl -fsI --max-time "${HF_PROBE_TIMEOUT:-5}" \
        https://huggingface.co >/dev/null 2>&1; then
        echo "huggingface.co is unreachable; setting HF_HUB_OFFLINE=1 so" \
            "models load from the local cache only (set HF_HUB_OFFLINE=0" \
            "to force online mode)."
        export HF_HUB_OFFLINE=1
    fi
fi

exec "$@"
