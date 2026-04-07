#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_MD="$ROOT_DIR/docs/architecture_diagrams.md"
OUTPUT_DIR="$ROOT_DIR/docs/assets"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

extract_mermaid_block() {
  local diagram_name="$1"
  local output_file="$2"

  awk -v marker="<!-- diagram: ${diagram_name} -->" '
    $0 == marker { found=1; next }
    found && /^```mermaid$/ { in_block=1; next }
    in_block && /^```$/ { exit }
    in_block { print }
  ' "$SOURCE_MD" > "$output_file"

  if [[ ! -s "$output_file" ]]; then
    echo "Could not extract Mermaid block for ${diagram_name} from ${SOURCE_MD}" >&2
    exit 1
  fi
}

render_diagram() {
  local diagram_name="$1"
  local input_file="$TMP_DIR/${diagram_name}.mmd"

  extract_mermaid_block "$diagram_name" "$input_file"

  npx --yes @mermaid-js/mermaid-cli \
    -i "$input_file" \
    -o "$OUTPUT_DIR/${diagram_name}.svg" \
    -b transparent
}

mkdir -p "$OUTPUT_DIR"

render_diagram "system_overview"
render_diagram "execution_flow"

echo "Exported architecture diagrams to $OUTPUT_DIR"
