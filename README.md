# BimCollab Community MCP

Piloter **BIMcollab Zoom** et analyser des **IFC** en langage naturel depuis **Claude Code**.

> *English summary — A Claude Code toolkit for BIMcollab Zoom (Windows): an IFC analysis MCP
> server (`bim-assistant`, IfcOpenShell), a CLI that reads/writes Zoom's native files (Smart Views,
> Clash Rules, Lists, Smart Properties, projects, Nightshift) and drives the **already open** Zoom
> session through UI Automation, plus a Claude Code skill. Install with `install.ps1`.*

Exemples de demandes :

- « Crée une vue avec les escaliers et compte les nez de marche des deux cages, sans les escaliers décoratifs »
- « Cubage total de béton classé par type d'objet, en liste Zoom colorée par type »
- « Liste des types de cloisons plâtre avec leurs surfaces en m², colorée par type »

Claude analyse l'IFC, génère la Smart View ou la Liste au format natif Zoom, l'importe **dans la session
Zoom ouverte** et l'affiche. Aucun clic n'est nécessaire.

## Contenu

| Dossier | Rôle |
|---|---|
| [`bim-assistant/`](bim-assistant) | Serveur **MCP** `bim-assistant` : outils `ifc_*` (analyse IFC), `zoom_*` (fichiers Zoom), `zoom_live_*` (session ouverte) |
| [`bimcollab-zoom/agent-harness/`](bimcollab-zoom/agent-harness) | CLI **`cli-anything-bczoom`** : formats natifs Zoom, Nightshift, pilotage live ([CLI-Anything](https://github.com/HKUDS/CLI-Anything)) |
| [`.claude/skills/cli-anything-bczoom/`](.claude/skills/cli-anything-bczoom/SKILL.md) | **Skill** Claude Code : règles d'usage du CLI |
| [`install.ps1`](install.ps1) | Installation en une commande |

## Prérequis

- Windows 10/11.
- [BIMcollab Zoom](https://www.bimcollab.com/en/products/bimcollab-zoom) 9.x installé. Il est facultatif pour la seule analyse IFC.
- [Python](https://www.python.org/downloads/) 3.11 ou plus récent (cochez « Add to PATH »).
- [Claude Code](https://claude.com/claude-code) (CLI, application de bureau ou extension IDE).

## Installation

```powershell
git clone https://github.com/tanguynoumea-collab/BimCollab_Community_MCP.git
cd BimCollab_Community_MCP
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Le script :

1. crée `.venv` ;
2. installe les deux paquets ;
3. écrit `.mcp.json` ;
4. installe le skill ;
5. vérifie que Zoom est détecté.

Ouvrez ensuite Claude Code **dans ce dossier** et acceptez le serveur `bim-assistant`.

Options :

| Option | Effet |
|---|---|
| `-Scope user` | Rend le serveur MCP et le skill disponibles dans **tous** vos projets, via `claude mcp add --scope user`. |
| `-Author "prenom.nom@entreprise.com"` | Auteur inscrit dans les fichiers Zoom générés. |
| `-Python "C:\...\python.exe"` | Utilise un interpréteur précis. |

Pour une installation manuelle, partez de [`.mcp.json.example`](.mcp.json.example).

Si Zoom est installé ailleurs que dans le dossier par défaut, définissez la variable d'environnement
`BCZOOM_EXE` avec le chemin vers `BIMcollab Zoom.exe`.

## Outils MCP (`bim-assistant`)

| Famille | Outils |
|---|---|
| Analyse IFC | `ifc_open`, `ifc_summary`, `ifc_spatial_tree`, `ifc_properties_catalog`, `ifc_count`, `ifc_distinct_values`, `ifc_query`, `ifc_element`, `ifc_quantities` (Qto par classe/type/matériau), `ifc_materials`, `ifc_audit`, `ifc_compare`, `ifc_export_table`, `ifc_list_models`, `ifc_close` |
| Géométrie réelle | `ifc_geometry` (volumes, surfaces, surfaces latérales, emprise, hauteurs ; `compare_qto` détecte les Qto faux), `ifc_distance`, `ifc_nearby`, `ifc_contact_area` (surfaces nettes : jonctions, bande sous faux plancher, plénum), `ifc_clearance` (hauteurs libres) |
| Fichiers Zoom | `zoom_smartview_create`, `zoom_smartview_color_by_values`, `zoom_list_create`, `zoom_smartproperty_create`, `zoom_clash_rule_create`, `zoom_project_create`, `zoom_library_summary`, `zoom_install_library`, `zoom_ifc_type`, `zoom_status`, `zoom_open`, `zoom_nightshift_run` |
| Session Zoom ouverte | `zoom_live_sessions`, `zoom_live_import` (avec `show`), `zoom_live_show_view`, `zoom_live_export`, `zoom_live_delete_set`, `zoom_live_run_list`, `zoom_live_run_clash`, `zoom_live_tab`, `zoom_live_menus`, `zoom_live_menu`, `zoom_live_tools`, `zoom_live_tool`, `zoom_live_search`, `zoom_live_screenshot` |

Le CLI s'utilise aussi seul. Exemples :

```powershell
.\.venv\Scripts\cli-anything-bczoom.exe --json app info
.\.venv\Scripts\cli-anything-bczoom.exe --json live sessions
.\.venv\Scripts\cli-anything-bczoom.exe --json live import vue.bcsv --show
```

Toutes les commandes sont listées dans le [SKILL.md](.claude/skills/cli-anything-bczoom/SKILL.md).

## Fonctionnement et bonnes pratiques

- **Formats natifs.** Les fichiers `.bcsv`, `.bcr`, `.bcl`, `.bcsp` et `.bcp` sont écrits au format exact de Zoom.
- **Session live.** Le pilotage passe par UI Automation (pywinauto). Les commandes live déplacent brièvement la vraie souris, car Zoom ignore les clics UIA : le poste doit être déverrouillé. Les libellés de menu suivent la langue de l'interface.
- **Règles génériques.** Les Smart Views et les Listes sont construites avec des règles fondées sur les propriétés (type IFC, nom de type, pset, matériau…), jamais élément par élément.
- **Listes.** Une Liste peut porter la même coloration qu'une Smart View.
- **Smart Properties.** Elles servent à fusionner des informations absentes nativement, par exemple `NetVolume` réparti sur plusieurs `Qto_*`.

## Sécurité

- **Zoom n'est jamais relancé** s'il est déjà ouvert, car cela créerait une seconde instance vide.
- **Aucun écrasement.** Les fichiers de bibliothèque de Zoom (`%APPDATA%\KUBUS\…`) ne sont jamais modifiés pendant que Zoom tourne : l'import live crée un nouvel ensemble.
- **Nightshift.** La commande `nightshift run` refuse de s'exécuter si Zoom est ouvert. Elle refuse aussi les scripts qui écrivent dans le cloud BIMcollab, sauf avec `--yes`.
- **Suppressions.** `zoom_live_delete_set` n'agit que sur demande explicite.

## Ressources (calculs géométriques)

Les maillages sont calculés une fois (noyau `hybrid-manifold-opencascade`, coordonnées recentrées
pour les grandes coordonnées nationales type LV95) puis mis en cache en mémoire et dans
`%LOCALAPPDATA%\bim-assistant\geomcache` (2 Go max, purge automatique). Garde-fous réglables par
variables d'environnement :

| Variable | Défaut | Rôle |
|---|---|---|
| `BIM_ASSISTANT_GEOM_THREADS` | min(4, cœurs/2) | Threads de tessellation |
| `BIM_ASSISTANT_RAM_MB` | 4000 | Budget mémoire du serveur, vérifié pendant les calculs |
| `BIM_ASSISTANT_MAX_ELEMENTS` | 20000 (5000 au-delà de 150 Mo d'IFC) | Éléments par appel |
| `BIM_ASSISTANT_GEOMCACHE_MB` | 2048 | Taille du cache disque |

## Limites

- Fonctionne sous Windows uniquement. Testé avec BIMcollab Zoom 9.5 (interface en français).
- Zoom n'offre pas d'API : le pilotage live dépend de son interface et peut nécessiter une adaptation après une mise à jour de Zoom.
- Les métrés sont calculés depuis l'IFC. Leur qualité dépend de celle de la maquette, notamment des quantités `Qto` et des matériaux.

## Licence

[MIT](LICENSE) : utilisation, modification et redistribution libres, sans garantie.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest bimcollab-zoom\agent-harness\cli_anything\bczoom\tests
```
