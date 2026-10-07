#!/usr/bin/env bash
# Deploys a Dokploy compose service through the Dokploy API and verifies the result:
# store the released compose file (COMPOSE_FILE) as the service's compose, trigger compose.deploy, wait until
# the new deployment is done, then expect HEALTH_URL to answer with status "ok" and the released version.
# No rollback by design (https://github.com/gotreat-de/website/blob/main/docs/specs/release-flow.md).
set -euo pipefail

die()
{
	echo "error: $*" >&2
	exit 1
}

for tool in curl jq; do
	command -v "$tool" > /dev/null || die "$tool is required"
done
for name in DOKPLOY_BASE_URL DOKPLOY_API_KEY DOKPLOY_COMPOSE_ID RELEASE_VERSION COMPOSE_FILE; do
	[ -n "${!name:-}" ] || die "$name is required"
done

DOKPLOY_BASE_URL=${DOKPLOY_BASE_URL%/}
HEALTH_URL=${HEALTH_URL:-}
DRY_RUN=${DRY_RUN:-false}
DEPLOY_TIMEOUT=${DOKPLOY_DEPLOY_TIMEOUT_SECONDS:-900}
HEALTH_TIMEOUT=${DOKPLOY_HEALTH_TIMEOUT_SECONDS:-180}
POLL_INTERVAL=${DOKPLOY_POLL_INTERVAL_SECONDS:-5}

# Plain HTTP is only accepted for a local fake (tests).
for url_name in DOKPLOY_BASE_URL HEALTH_URL; do
	url=${!url_name}
	[ -n "$url" ] || continue
	[[ "$url" == https://* || "$url" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?(/|$) ]] \
		|| die "$url_name must use HTTPS"
	[[ "$url" != *[$'\r\n\t ']* ]] || die "$url_name must not contain whitespace"
done
[[ "$DOKPLOY_API_KEY" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "DOKPLOY_API_KEY has an invalid format"
[[ "$DOKPLOY_COMPOSE_ID" =~ ^[A-Za-z0-9_-]+$ ]] || die "DOKPLOY_COMPOSE_ID contains unsupported characters"
[[ "$RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]] \
	|| die "RELEASE_VERSION must be a semantic version without a v prefix"
[[ "$DEPLOY_TIMEOUT" =~ ^[1-9][0-9]*$ && "$HEALTH_TIMEOUT" =~ ^[1-9][0-9]*$ ]] \
	|| die "timeouts must be positive integers"
[[ "$POLL_INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "DOKPLOY_POLL_INTERVAL_SECONDS must be a number"
[ "$DRY_RUN" = true ] || [ "$DRY_RUN" = false ] || die "DRY_RUN must be true or false"

# The released compose file pins its images to the release (release-please rewrites the annotated `image:`
# lines). One that does not - a stale `:latest`, another version - would deploy something else than was released.
[ -s "$COMPOSE_FILE" ] || die "COMPOSE_FILE $COMPOSE_FILE is missing or empty"
release_image_found=false
while IFS= read -r line; do
	if [[ "$line" =~ ^[[:space:]]*image:[[:space:]]*[^[:space:]#]+:v([^[:space:]#]+) ]] \
		&& [ "${BASH_REMATCH[1]}" = "$RELEASE_VERSION" ]; then
		release_image_found=true
	fi
done < "$COMPOSE_FILE"
[ "$release_image_found" = true ] || die "COMPOSE_FILE has no image tagged v$RELEASE_VERSION"

WORK_DIR=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/dokploy-deploy.XXXXXX")
trap 'rm -rf -- "$WORK_DIR"' EXIT

# The key goes through a config file so it never shows up in the process list or in logs.
CURL_CONFIG=$WORK_DIR/curl.conf
(
	umask 077
	printf 'header = "Accept: application/json"\nheader = "x-api-key: %s"\n' "$DOKPLOY_API_KEY" > "$CURL_CONFIG"
)

# try_api METHOD ENDPOINT OUTPUT_FILE [PAYLOAD_FILE] - like api, but returns 1 and sets the
# global API_ERROR instead of dying, so a caller that can retry (the wait loop) does not have to.
try_api()
{
	local method=$1 endpoint=$2 output=$3 payload=${4:-} code
	local -a args=(
		--silent --show-error --connect-timeout 10 --max-time 60
		--config "$CURL_CONFIG" --output "$output" --write-out '%{http_code}'
	)
	if [ "$method" = POST ]; then
		args+=(--request POST --header 'Content-Type: application/json' --data-binary "@$payload")
	fi
	if ! code=$(curl "${args[@]}" "$DOKPLOY_BASE_URL/api/$endpoint"); then
		API_ERROR="Dokploy API request failed: $method /api/$endpoint"
		return 1
	fi
	if [[ "$code" != 2?? ]]; then
		API_ERROR="Dokploy returned HTTP $code for $method /api/$endpoint: $(jq -r '.message // "no message"' "$output" 2> /dev/null || echo "unreadable body")"
		return 1
	fi
}

# api METHOD ENDPOINT OUTPUT_FILE [PAYLOAD_FILE] - dies on transport errors and non-2xx answers.
api()
{
	try_api "$@" || die "$API_ERROR"
}

# try_list_deployments OUTPUT_FILE - like list_deployments, but returns 1 and sets the global
# API_ERROR instead of dying.
try_list_deployments()
{
	try_api GET "deployment.allByCompose?composeId=$DOKPLOY_COMPOSE_ID" "$1" || return 1
	if ! jq -e 'type == "array"' "$1" > /dev/null 2>&1; then
		API_ERROR="Dokploy returned an invalid deployment list"
		return 1
	fi
}

list_deployments()
{
	try_list_deployments "$1" || die "$API_ERROR"
}

BEFORE=$WORK_DIR/before.json
list_deployments "$BEFORE"
[ "$(jq -r '.[0].status // empty' "$BEFORE")" != running ] || die "another Dokploy deployment is already running"

# stored_compose_matches FILE - whether the compose service's stored compose (compose.one, in FILE) is the
# released one, as raw source. Trailing newlines do not count.
stored_compose_matches()
{
	jq -e --rawfile released "$COMPOSE_FILE" \
		'.sourceType == "raw" and ((.composeFile // "") | sub("\n+$"; "")) == ($released | sub("\n+$"; ""))' \
		"$1" > /dev/null
}

STORED=$WORK_DIR/stored.json
api GET "compose.one?composeId=$DOKPLOY_COMPOSE_ID" "$STORED"

if [ "$DRY_RUN" = true ]; then
	if stored_compose_matches "$STORED"; then
		echo "dry run: Dokploy stores the released compose file"
	else
		echo "dry run: Dokploy stores another compose file (source $(jq -r '.sourceType // "unknown"' "$STORED")); a deploy replaces it"
	fi
	echo "dry run: Dokploy API access, the stored compose and the deployment list verified; nothing deployed"
	exit 0
fi

# Dokploy runs the compose it stores, never the repo's: without this step a stale copy - an old module, a
# `:latest` image - is what runs. The repo's compose.yml is the only source.
UPDATE=$WORK_DIR/update.json
jq -n --arg composeId "$DOKPLOY_COMPOSE_ID" --rawfile composeFile "$COMPOSE_FILE" \
	'{composeId: $composeId, sourceType: "raw", composeFile: $composeFile}' > "$UPDATE"
api POST compose.update "$WORK_DIR/update-response.json" "$UPDATE"
api GET "compose.one?composeId=$DOKPLOY_COMPOSE_ID" "$STORED"
stored_compose_matches "$STORED" || die "Dokploy did not store the released compose file"
echo "Dokploy stores the released compose file of v$RELEASE_VERSION"

PAYLOAD=$WORK_DIR/deploy.json
jq -n \
	--arg composeId "$DOKPLOY_COMPOSE_ID" \
	--arg title "Release v$RELEASE_VERSION from GitHub Actions" \
	--arg description "run=${GITHUB_RUN_ID:-local} attempt=${GITHUB_RUN_ATTEMPT:-1} sha=${RELEASE_SHA:-unknown}" \
	'{composeId: $composeId, title: $title, description: $description}' > "$PAYLOAD"
api POST compose.deploy "$WORK_DIR/deploy-response.json" "$PAYLOAD"
# Dokploy answers with composeId, message and success - no deployment id (seen live with v0.4.0), hence the
# heuristic below. The answer's shape stays logged (keys, no values) in case that ever changes.
echo "Dokploy accepted the deployment request for v$RELEASE_VERSION (answer: $(jq -r 'if type == "object" then keys | join(", ") else type end' "$WORK_DIR/deploy-response.json" 2> /dev/null || echo unreadable))"

# The new deployment is the newest entry that did not exist before the request - this does not
# depend on Dokploy echoing the title back.
# Residual race: a deployment somebody else starts between the snapshot above and the first poll
# would be mistaken for ours. The workflow's concurrency group serializes our own runs and the
# running-gate above narrows the rest.
deployment_id=
deadline=$((SECONDS + DEPLOY_TIMEOUT))
while :; do
	((SECONDS < deadline)) || die "Dokploy deployment did not finish within $DEPLOY_TIMEOUT seconds"
	# A transient listing failure (transport error, non-2xx, a body that is not a JSON array) does
	# not fail the job - the deployment carries on regardless and a re-dispatch would only hit
	# "already running". Warn and try again until the deadline above.
	if ! try_list_deployments "$WORK_DIR/now.json"; then
		echo "warning: $API_ERROR" >&2
		sleep "$POLL_INTERVAL"
		continue
	fi
	deployment=$(jq -c --slurpfile before "$BEFORE" \
		'[$before[0][].deploymentId] as $known | [.[] | select(.deploymentId | IN($known[]) | not)] | first // empty' \
		"$WORK_DIR/now.json")
	if [ -n "$deployment" ]; then
		deployment_id=$(jq -r '.deploymentId // empty' <<< "$deployment")
		status=$(jq -r '.status // empty' <<< "$deployment")
		echo "Dokploy deployment ${deployment_id:-unknown}: ${status:-unknown}"
		case "$status" in
			done) break ;;
			error | cancelled)
				die "Dokploy deployment $status: $(jq -r '(.errorMessage // "") | if . == "" then "no error message returned" else . end' <<< "$deployment")"
				;;
		esac
	fi
	sleep "$POLL_INTERVAL"
done

if [ -n "$HEALTH_URL" ]; then
	deadline=$((SECONDS + HEALTH_TIMEOUT))
	until curl --fail --silent --connect-timeout 5 --max-time 10 --output "$WORK_DIR/health.json" "$HEALTH_URL" \
		&& jq -e --arg version "$RELEASE_VERSION" '.status == "ok" and .version == $version' \
			"$WORK_DIR/health.json" > /dev/null; do
		((SECONDS < deadline)) \
			|| die "$HEALTH_URL did not report status ok with version $RELEASE_VERSION within $HEALTH_TIMEOUT seconds (last answer: $(head -c 300 "$WORK_DIR/health.json" 2> /dev/null || echo none))"
		sleep "$POLL_INTERVAL"
	done
	echo "$HEALTH_URL reports status ok with version $RELEASE_VERSION"
fi

[[ "$deployment_id" =~ ^[A-Za-z0-9_-]+$ ]] || deployment_id=unknown
if [ -n "${GITHUB_OUTPUT:-}" ]; then
	echo "deployment_id=$deployment_id" >> "$GITHUB_OUTPUT"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
	{
		echo "## Deploy"
		echo
		echo "| Field | Value |"
		echo "| --- | --- |"
		echo "| Version | v$RELEASE_VERSION |"
		echo "| Dokploy deployment | $deployment_id |"
		echo "| Health | ${HEALTH_URL:-not checked (no HTTP endpoint)} |"
	} >> "$GITHUB_STEP_SUMMARY"
fi
echo "v$RELEASE_VERSION deployed and verified"
