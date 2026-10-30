# Postmuxarr

Web app Docker pour les membres de HD-Forever, en deux volets :

- **Remux** : Blu-ray (dossier BDMV ou ISO) → MKV avec mkvmerge. Choix de la playlist, des pistes, des langues et des
  drapeaux, nom de release au format scene avec ton tag, BDInfo, MediaInfo, torrent créé et seedé dans qBittorrent,
  hardlink vers la bibliothèque, vérification des doublons sur HD-Forever avant de remuxer.
- **Repost** : reprend des releases déjà en seed dans qBittorrent et les prépare pour HD-Forever. Les fichiers des
  autres seeds ne sont jamais modifiés.
  - Inventaire lu dans **qBittorrent** (les cross-seeds d'une même release sont regroupés), ou dans
    [Nettoyarr](https://github.com/Gusdezup/Nettoyarr) s'il est installé (titres et trackers via Radarr, Sonarr,
    Prowlarr). Nettoyarr est facultatif.
  - Releases repostables : un film en MKV, une saison complète (dossier d'épisodes MKV), un FULL Blu-ray (BDMV ou ISO).
  - Filtres à règles (tracker, catégorie, taille, nom, résolution, contenu…) et champ « Règles HDF » : ce que les
    règles d'upload refusent d'après le nom et le contenu (DVD, CAM/TS, WEBRip, 720p, épisode seul, mHD hors teams
    autorisées…).
  - Analyse MediaInfo d'une release : débit ou CRF minimum, réglages x264, 10 bits, formats audio, VF, VO et
    sous-titres français, core DTS, TrueHD sans AC3, chapitres.
  - Vérification sur HD-Forever par son API : doublon, identique (cross-seed sans rien télécharger), sans seeder.
  - Torrent créé sur les mêmes fichiers, MediaInfo ou BDInfo prêt, puis upload depuis le bouton du job.

Les traitements longs sont des jobs côté serveur : le navigateur peut être fermé.

> **Sécurité** : l'app n'a pas d'authentification. Ne l'expose pas hors de ton réseau local (pas de redirection de
> port, pas de reverse proxy public sans authentification devant).

## Installation

1. Copier ce dossier dans le dossier du stack (par exemple `stack/postmuxarr`).
2. `cp config.example.yml config.yml`. Ce fichier n'est jamais versionné. Il garde les conventions de nommage et
   l'URL d'annonce de chaque tracker (avec ta passkey) ; tout le reste se règle dans l'interface.
3. Ajouter le service de `docker-compose.example.yml` au `docker-compose.yml` du stack, et adapter les chemins.
   Les médias doivent être montés au même chemin que sur l'hôte, en un seul montage (hardlinks).
4. `sudo docker compose up -d --build postmuxarr`, puis http://NAS:8790
5. Onglet **Réglages** : connexion à qBittorrent (avec le bouton Tester, qui vérifie aussi la correspondance des
   chemins), clé TMDB, clé API HD-Forever, et Nettoyarr si tu l'utilises.

## Réglages

L'onglet Réglages regroupe :

- **qBittorrent** : adresse, clé API (qBittorrent 5.2 et plus) ou utilisateur et mot de passe, catégories et tags,
  correspondance des chemins (dossier vu par Postmuxarr → même dossier vu par qBittorrent).
- **Services** : clé TMDB, clé API HD-Forever (HDF → Intégrations → API), source de l'inventaire du Repost,
  Nettoyarr (facultatif).
- **Remux et règles d'upload HDF** : tag des releases, team de la dérogation au doublon, tolérance de taille,
  termes interdits, teams mHD, seuils de débit et de CRF, réglages x264 minimum, formats audio admis, chapitres…
  Valeurs par défaut : règles d'upload de HD-Forever d'octobre 2026. Seules tes modifications sont enregistrées ;
  « Critères par défaut » y revient.

Les réglages sont stockés dans `/data` (base de l'app). Les clés et mots de passe ne sont jamais renvoyés à la page.
Les variables d'environnement (`QBT_API_KEY`, `TMDB_API_KEY`, `HDF_API_KEY`, `NETTOYARR_USER`, `NETTOYARR_PASS`)
et `config.yml` restent acceptés en repli : l'interface est prioritaire.

## Mises à jour

- `config.yml` modifié : rien à faire, il est relu à chaque utilisation (sauf `paths`, lu au démarrage :
  `sudo docker compose restart postmuxarr`).
- `app/` modifié : `sudo docker compose up -d --build postmuxarr` (le Dockerfile copie `app/` dans l'image),
  quand aucun job n'est en cours.

## Structure

- `app/main.py` : backend FastAPI
- `app/static/index.html` : interface
- `Dockerfile` : Python 3.12 Alpine + mkvtoolnix, mediainfo, 7-Zip, go-bdinfo
- `config.example.yml` : modèle de configuration
- `docker-compose.example.yml` : service à ajouter au stack
