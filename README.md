# Omnix community worlds

Worlds for the [Omnix](https://github.com/autonomx/omnix) RPG, shared by the people who made them.

Omnix reads [`index.json`](index.json) and lists these worlds under **Worlds → Community**. Each world's file
(an Omnix world bundle: its lore, maps, openings and images) is attached to a release of this repository, and the
index records its size and SHA-256. Omnix downloads only from these releases and checks both before importing.

## Playing a shared world

1. In Omnix, open **Worlds → Community** and choose **Download**.
2. The world joins **My worlds**, marked *Shared · review to play*. Open it in the editor to look through it.
3. Choose **Review & adopt**. A world made in another Omnix is never played until someone adopts it, because its
   text becomes what the storyteller reads during your campaigns.

## Sharing your world

In Omnix, open the world's **More** menu and choose **Share to community**. Sign in to GitHub once (a token with
the `public_repo` scope). Omnix then:

1. uploads the world's file to a release on your fork of this repository;
2. opens a pull request adding `submissions/<id>.json`, which names that file, its size and its SHA-256.

The **Check world submission** workflow downloads the file and checks it. Once a maintainer merges the pull request,
the **Publish worlds** workflow attaches the file to a release here and lists it in `index.json`. By sharing a world you let anyone download and play it. Sharing the same
world again (same id, same author) publishes a new version. Only its author can update a world.

## For maintainers

- Merge a world pull request only when its check passes, and after a look at the world. Changes outside
  `submissions/` are refused by the check.
- To list a bundle you have locally: `python tools/catalog.py publish-file path/to/world.omnix-world.zip --id <id> --author <github-login>`
  (needs the GitHub CLI signed in with write access), then commit `index.json`.
- To remove a world, delete its entry from `index.json`; Omnix stops offering it. Delete its release too to remove the file.
