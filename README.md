# ac-rennes-mail-extract

`ac-rennes-mail-extract` permet l'extraction d'une archive de boîte aux lettres 
de l'Académie de Rennes (export Oracle / Sun Java System Messaging Server) vers 
une arborescence de fichiers `.eml` classés par année, accompagnée d'un index 
Excel (`.xlsx`) — en vue d'un archivage pérenne (contexte SSI / archivage).

## Documentation utilisateur

### Prérequis

Windows 11.

### Installation

1. Récupérer le dernier exécutable sur https://github.com/pascalaubry/ac-rennes-mail-extract/releases (`ac-rennes-mail-extract-x.y.z.exe`) ;
2. L'installer dans un répertoire dédié (par exemple `mail_extract`).

### Utilisation

1. Récupérer la boite à lettres à archiver auprès des responsables de la messagerie ;
2. La déposer dans le répertoire `mail_extract/archives`.
3. Lancer le programme

```
ac-rennes-mail-extract-x.y.z.exe
```

### Fonctionnement

S'il n'y a qu'une archive dans `archives/`, elle est traitée directement.
S'il y en a plusieurs, un menu propose de choisir.

Une fois l'archive choisie, un menu demande quelles années traiter :

1. toutes les années (jusqu'à l'année en cours) ;
2. toutes les années passées (jusqu'à l'année précédente, donc sans l'année en cours) ;
3. l'année précédente seulement.

Les messages sans date exploitable sont toujours traités, quel que soit ce choix.

Une barre de progression s'affiche (pourcentage calculé sur la taille
compressée, estimation du temps restant, nombre de messages traités).
`Ctrl-C` interrompt proprement le traitement.

> Relancer le traitement **efface** d'abord `tmp/<compte>/` et `output/<compte>/`
> puis recommence de zéro (`<compte>` = nom de l'archive sans extension).

Les options suivantes sont disponibles :

| Option           | Défaut       | Rôle                                                       |
|------------------|--------------|------------------------------------------------------------|
| `--archives-dir` | `./archives` | Dossier contenant l'archive `<compte>.tgz`.                |
| `--tmp-dir`      | `./tmp`      | Dossier d'extraction temporaire (vidé en fin de course).   |
| `--output-dir`   | `./output`   | Dossier contenant les fichiers à archiver.                 |
| `--limit N`      | (aucune)     | S'arrêter après `N` messages (tests). `0` = pas de limite. |
| `-h`, `--help`   |              | Aide.                                                      |

Exemple :

```
ac-rennes-mail-extract-x.y.z.exe --output-dir D:\archivage\sortie --limit 10000
```

### Résultat produit

Le résultat du programme se présente sous cette hiérarchie de dossier et fichiers.

```
output/
└── <compte>/
    ├── <compte>.xlsx          ← index global (tous les messages)
    ├── <compte>-2018.xlsx     ← index de l'année 2018 uniquement
    ├── <compte>-2018.zip      ← contient 2018/<hiérarchie de dossiers>/*.eml
    ├── <compte>-2019.xlsx
    ├── <compte>-2019.zip
    ├── <compte>-unknown.xlsx  ← messages sans date exploitable
    └── <compte>-unknown.zip
```

Les fichiers des archives par année sont organisés de la manière suivante :
- `<année>/<hiérarchie du dossier de messagerie>/<AAAAMMJJ-HHMMSS>_<sujet>.eml`.

En cas de conflit de nom (même sujet et même date), les fichiers sont suffixées `_1`, `_2`, …

Le classeur global `output\<compte>\<compte>.xlsx` et les classeurs annuels
`output\<compte>\<compte>-<année>.xlsx` (une feuille, ligne d'en-tête figée)
s'ouvrent directement dans Excel et contiennent une ligne par message. Les
classeurs annuels ont exactement les mêmes colonnes que l'index global, filtrées
sur l'année.

| Colonne             | Contenu                                                           |
|---------------------|-------------------------------------------------------------------|
| `year`              | Année du message (`AAAA`) ou `unknown`.                           |
| `folder`            | Hiérarchie du dossier de messagerie (ex. `Support/Applications`). |
| `date`              | Date du message au format ISO 8601 (vide si inconnue).            |
| `subject`           | Sujet du message.                                                 |
| `from`              | Adresse de l'expéditeur.                                          |
| `to` / `cc` / `bcc` | Adresses des destinataires, séparées par des virgules.            |
| `output_file`       | Chemin du `.eml`, relatif à `output/<compte>/`.                   |

### Format d'entrée attendu pour les archives

L'archive se décompresse en `store*/part*/<compte>.export/`. Dans ce dossier :

- chaque dossier de messagerie est **un fichier mbox** (`INBOX`, `Sent`, …),
  les messages étant séparés par des lignes `From <adresse> <date>` ;
- les sous-dossiers sont des **sous-répertoires** du même nom ;
- un fichier `<Nom>.msg` **non vide** contient les messages propres au dossier
  parent.

## Documentation développeur

### Prérequis

- **Python 3.13** ou plus récent.

### Installation

```
py -3.13 -m venv .venv-3.13
.venv-3.13\Scripts\activate
pip install -e .
```

### Utilisation

1. Placer l'archive à traiter dans le dossier `archives/`
   (`.tgz`, `.tar.gz` ou `.tar`). Exemple : `archives/paubry.tgz`.
2. Lancer l'application :

   ```
   python src\main.py
   ```

### Générer un exécutable autonome localement

```
pip install -e ".[build_exe]"
python scripts\build_exe.py
```

`scripts/build_exe.py` appelle PyInstaller (mode un seul fichier, console).

### Publier une nouvelle version en ligne

Créer un tag du nom de la version, par exemple :

```
git tag --file=RELEASE_NOTES.md 1.1.0
```

Puis pousser le tag sur le dépôt :

```
git push origin tag 1.1.0
```

La nouvelle version est disponible sur https://github.com/pascalaubry/ac-rennes-mail-extract/releases.

### Environnement de développement

```
pip install -e ".[lint]"
mypy src
ruff check
ruff format
```

`pyproject.toml` regroupe la configuration (`[tool.mypy]`, `[tool.ruff]`) et les
extras `lint` et `build_exe`.
