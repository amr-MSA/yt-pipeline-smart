from pathlib import Path

workflow = Path(__file__).parents[1].joinpath('.github/workflows/long_pipeline.yml').read_text()
required_fragments = [
    'uses: denoland/setup-deno@v2',
    'deno-version: v2.3.0',
    'pip install -q -r requirements.txt',
    'if: always()',
    'git add -A state/',
]
for fragment in required_fragments:
    assert fragment in workflow, fragment
assert workflow.count('${{ secrets.LONG_BOT_TOKEN }}') == 1
assert workflow.count('${{ secrets.YOUTUBE_TOKEN_JSON }}') == 1
assert workflow.count('${{ secrets.YT_COOKIES }}') == 1
assert 'long_source_history' not in workflow
print('workflow structure: OK')
