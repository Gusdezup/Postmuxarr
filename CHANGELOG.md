# Changelog

Numérotation [SemVer](https://semver.org/lang/fr/). Tant que la version est en 0.x, une version mineure (0.2.0,
0.3.0…) peut demander une modification de `config.yml` ou du service Docker : c'est alors signalé dans ses notes.

Les changements à venir s'ajoutent sous « Non publié » ; `scripts/release.sh` en fait la section de la version.

## [Non publié]

## [0.1.0] - 2026-10-31

Première version publique.

### Remux

- Blu-ray en dossier BDMV ou ISO vers MKV avec mkvmerge : playlist, pistes, langues, drapeaux, chapitres.
- Nom de release au format scene avec ton tag (réglable dans **Réglages**).
- BDInfo, MediaInfo, torrent créé et seedé dans qBittorrent, hardlink vers la bibliothèque.
- Vérification des doublons sur HD-Forever avant de remuxer, analyse d'un dossier entier de Blu-ray.

### Repost

- Inventaire lu dans qBittorrent, cross-seeds d'une même release regroupés, choix de la version à reposter.
- Radarr et Sonarr facultatifs (plusieurs instances) pour le titre et la fiche TMDB exacts.
- Films en MKV, saisons complètes, FULL Blu-ray (BDMV ou ISO).
- Filtres à règles et règles d'upload HDF (nom, contenu, analyse MediaInfo).
- Vérification sur HD-Forever par son API : doublon, identique (cross-seed), sans seeder.

### Réglages et accès

- qBittorrent, TMDB, HD-Forever, Radarr et Sonarr réglés dans l'interface.
- Critères des règles d'upload HDF modifiables.
- Protection facultative par mot de passe, clé API pour les userscripts.

### Userscripts

- Disponibilité sur HD-Forever depuis PTP, ABN, TR4KER, Nostradamus et C411.
- Remplissage du formulaire d'upload de HD-Forever depuis un job.

### Docker

- Image publiée sur `ghcr.io/gusdezup/postmuxarr` (amd64).
