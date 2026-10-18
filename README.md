# Postmuxarr

Web app pour un NAS (Docker) qui fait deux choses :

- **Remux** : Blu-ray (dossier BDMV ou ISO) → MKV avec mkvmerge. Choix de la playlist, des pistes, des langues et des
  drapeaux, nom de release au format scene, BDInfo, MediaInfo, torrent créé et seedé dans qBittorrent, hardlink
  vers la bibliothèque Plex, vérification des doublons sur HD-Forever.
- **Repost** : reprend des releases déjà en seed (inventaire lu dans Nettoyarr), vérifie HD-Forever par son API,
  puis cross-seede ou prépare l'upload. Les fichiers des autres seeds ne sont jamais modifiés.

Les traitements longs sont des jobs côté serveur : le navigateur peut être fermé.

## Installation

1. Copier ce dossier dans le dossier du stack (ici `stackmux/postmuxarr`).
2. `cp config.example.yml config.yml`, puis adapter `config.yml` (chemins, qBittorrent, URL d'announce avec la passkey).
   `config.yml` n'est jamais versionné.
3. Secrets dans le `.env` du stack :
   `QBT_API_KEY` (qBittorrent ≥ 5.2), `TMDB_API_KEY`, `HDF_API_KEY`, et `NETTOYARR_USER` / `NETTOYARR_PASS`
   si l'authentification de Nettoyarr est active.
4. Ajouter le service de `docker-compose.example.yml` au `docker-compose.yml` du stack.
   `/volume1/media` doit être monté au même chemin dans tous les conteneurs (hardlinks).
5. `sudo docker compose up -d --build postmuxarr`, puis http://NAS:8790

## Mises à jour

- `config.yml` modifié : `sudo docker compose restart postmuxarr`
- `app/` modifié : `sudo docker compose up -d --build postmuxarr` (le Dockerfile copie `app/` dans l'image),
  quand aucun remux n'est en cours.

## Structure

- `app/main.py` : backend FastAPI
- `app/static/index.html` : interface
- `Dockerfile` : Python 3.12 Alpine + mkvtoolnix, mediainfo, 7-Zip, go-bdinfo
- `config.example.yml` : modèle de configuration
