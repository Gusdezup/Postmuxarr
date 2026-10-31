#!/usr/bin/env bash
# Publie une version : ./scripts/release.sh 0.2.0
# - « ## [Non publié] » de CHANGELOG.md devient « ## [0.2.0] - date » (ou section [0.2.0] déjà écrite à la main)
# - app/VERSION mis à jour, commit « Version 0.2.0 », tag v0.2.0, push de main et du tag
# - GitHub Actions publie alors l'image ghcr.io/gusdezup/postmuxarr et la GitHub Release
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "Erreur : $*" >&2; exit 1; }

ver="${1:-}"; ver="${ver#v}"
[[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage : $0 X.Y.Z (ex. 0.2.0)"
tag="v$ver"
cur="$(tr -d '[:space:]' < app/VERSION 2>/dev/null || echo 0.0.0)"

[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "pas sur la branche main"
[ -z "$(git status --porcelain)" ] || die "modifications non commitées (git status)"
git fetch -q origin main --tags
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main n'est pas à jour avec origin/main (pull ou push d'abord)"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "le tag $tag existe déjà"
if [ "$cur" != "$ver" ] && [ "$(printf '%s\n%s\n' "$cur" "$ver" | sort -V | tail -1)" != "$ver" ]; then
  die "$ver est inférieure à la version actuelle ($cur)"
fi

today="$(date +%F)"
section() { awk -v h="$1" 'index($0, h) == 1 { on = 1; next } on && /^## \[/ { exit } on' CHANGELOG.md | grep -c '[^[:space:]]' || true; }

if grep -q "^## \[$ver\]" CHANGELOG.md; then
  [ "$(section "## [$ver]")" -gt 0 ] || die "section [$ver] vide dans CHANGELOG.md"
  [ "$(section "## [Non publié]")" -eq 0 ] || die "CHANGELOG.md : « Non publié » n'est pas vide alors que [$ver] existe déjà"
else
  [ "$(section "## [Non publié]")" -gt 0 ] || die "rien sous « ## [Non publié] » dans CHANGELOG.md"
  awk -v v="$ver" -v d="$today" '
    !done && /^## \[Non publié\]/ { print; print ""; print "## [" v "] - " d; done = 1; next }
    { print }
  ' CHANGELOG.md > CHANGELOG.md.tmp && mv CHANGELOG.md.tmp CHANGELOG.md
fi
printf '%s\n' "$ver" > app/VERSION

echo "──── Notes de $tag"
awk -v h="## [$ver]" 'index($0, h) == 1 { on = 1; next } on && /^## \[/ { exit } on' CHANGELOG.md
echo "────"
read -r -p "Publier $tag (actuelle : $cur) ? [o/N] " ok
if [[ ! "$ok" =~ ^[oOyY]$ ]]; then
  git checkout -q -- CHANGELOG.md app/VERSION
  echo "Abandon, rien n'a été modifié."; exit 1
fi

git add CHANGELOG.md app/VERSION
git diff --cached --quiet || git commit -q -m "Version $ver"
git tag -a "$tag" -m "Postmuxarr $ver"
git push --atomic -q origin main "$tag"
echo "Publié : $tag. Build et release : https://github.com/Gusdezup/postmuxarr/actions"
