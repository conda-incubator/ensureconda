#!/usr/bin/env xonsh
$PROJECT = 'ensureconda'
$ACTIVITIES = [
	'version_bump',
	'changelog',
	'tag',
	'pypi',
	'push_tag',
	# 'ghrelease'
]

$GITHUB_ORG = 'mariusvniekerk'
$GITHUB_REPO = 'ensureconda'

$VERSION_BUMP_PATTERNS = [
   # These note where/how to find the version numbers
   ('ensureconda/__init__.py', '__version__\s*=.*', '__version__ = "$VERSION"'),
   ('pyproject.toml', 'version\s*=.*', 'version = "$VERSION")
   ('setup.py', 'version\s*=.*,', 'version="$VERSION",'),

]

$CHANGELOG_FILENAME = 'docs/changelog.md'
$CHANGELOG_TEMPLATE = 'TEMPLATE.md'
$CHANGELOG_HEADER = '''
# $RELEASE_DATE $VERSION:

'''
