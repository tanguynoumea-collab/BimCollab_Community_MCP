# bim-assistant

Serveur MCP local : un assistant de maquette qui **analyse les IFC** (IfcOpenShell) et
**pilote BIMcollab Zoom** (via `cli-anything-bczoom`, dans `../bimcollab-zoom`).

## Outils

| Outil | Rôle |
|---|---|
| `ifc_open`, `ifc_list_models`, `ifc_close` | Charger les maquettes (gardées en mémoire) |
| `ifc_summary`, `ifc_spatial_tree` | Vue d'ensemble : logiciel auteur, unités, niveaux et altitudes, éléments par classe, psets, géoréférencement |
| `ifc_properties_catalog` | Quelles infos existent : chaque `Pset.Prop` avec taux de remplissage et valeurs fréquentes |
| `ifc_count`, `ifc_distinct_values`, `ifc_query`, `ifc_element` | Comptages groupés, valeurs distinctes, listes d'éléments, fiche complète d'un élément |
| `ifc_quantities` | Sommes des Qto (m, m², m³) par classe, niveau, type… |
| `ifc_audit` | Contrôle qualité + infos obligatoires manquantes |
| `ifc_compare` | Différences entre deux versions (ajouts, suppressions, propriétés modifiées) |
| `ifc_export_table` | Export Excel/CSV |
| `zoom_status`, `zoom_open`, `zoom_project_create` | Zoom installé, ouvrir des IFC/.bcp, créer un projet |
| `zoom_smartview_create`, `zoom_smartview_color_by_values` | Smart Views ; la seconde colore automatiquement chaque valeur trouvée dans l'IFC |
| `zoom_clash_rule_create`, `zoom_library_summary` | Règles de clash, lecture de bibliothèques |
| `zoom_install_library` | Pousse des ensembles dans la bibliothèque vivante de Zoom (Zoom fermé, sauvegarde auto) |
| `zoom_nightshift_run` | Exécution automatique de Zoom (ouverture, connexion, issues, listes, synchro) |
| `zoom_live_sessions`, `zoom_live_import`, `zoom_live_show_view`, `zoom_live_export` | Session Zoom **déjà ouverte** : importer (et afficher) une Smart View, afficher une vue, exporter une bibliothèque |
| `zoom_live_run_list`, `zoom_live_run_clash`, `zoom_live_tab` | Lancer une liste ou une règle de clash, changer d'onglet |
| `zoom_live_menus`, `zoom_live_menu`, `zoom_live_tools`, `zoom_live_tool`, `zoom_live_search` | Commandes de menu, barre d'outils, recherche |
| `zoom_live_screenshot` | Capture de la vue 3D, renvoyée en image |

Sélecteurs : `"IfcDoor, Données.#EI_FEU_CIBLE=EI30"`, psets avec espaces entre guillemets :
`"IfcDoor, \"Protection contre les incendies\".#CIBLE_RES_FEU=EI30"`.

## Installation

```bash
py -3.14 -m venv .venv
.venv/Scripts/python -m pip install -e ../bimcollab-zoom/agent-harness -e .
```

Claude Code : déclaré dans `../.mcp.json` (portée projet `PROJET MCP`).
Claude Desktop : ajouter le même bloc `bim-assistant` dans `%APPDATA%\Claude\claude_desktop_config.json`.

## Sécurité

* Écriture vers le cloud BIMcollab (issues, listes, synchro) : refusée sans `confirm_cloud_write=true`.
* `zoom_install_library` / `zoom_nightshift_run` : refusés si Zoom est ouvert.
* `zoom_open` : refusé si Zoom est ouvert (relancer l'exe crée une seconde instance vide) ; utiliser `zoom_live_*`.
* `zoom_live_*` : UI Automation sur la fenêtre existante (pywinauto) ; bougent brièvement la souris, session Windows déverrouillée requise.
* Les maquettes ne sont jamais modifiées (lecture seule).

## Tests

```bash
.venv/Scripts/python -m pytest tests -v          # 31 tests (IFC synthétique + client MCP réel)
.venv/Scripts/python -m pytest tests -m gui -s   # lance vraiment Zoom (Zoom fermé)
```

Limites : pas de recalcul géométrique (les quantités viennent des Qto de l'IFC) ; la détection
de clash elle-même s'exécute dans Zoom.
