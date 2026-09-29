# README and Documentation Structure

Primary-source research on what Beehive's README should hold, what belongs in other documents, and
how to write both in plain words.

Date: 2026-09-30

## Executive summary

A README is a project's front door, not its manual. GitHub, Open Source Guides and Write the Docs
agree that it should say what the project does, why it is useful, how to get started and where to
get help, then link to longer documents. Diátaxis adds that tutorials, how-to guides, reference and
explanation meet different needs and go wrong when they are mixed.

Beehive's README mixes all four. At 365 lines and 2,887 words it is the wordiest of the 11 READMEs
compared here (the other ten have a median of about 950 words), and 59% of its words sit in three
sections: Research Sessions, Configuration, and Collect and digest. It uses internal model names,
and its quick start runs the test suite but stops before anything appears on screen.

The three most important findings:

1. **On GitHub, the README is not the first screen.** Measured in a signed-out browser at 1440×900,
   the About box (description and topics) starts 206 px down the page. The README starts at 1,225 px,
   its first sentence at 1,667 px, and it renders about 13 screens tall. Visitors read the About
   description first, so it and the README's opening must say the same thing, briefly.
2. **Split by reader.** People deciding whether to try Beehive, people using it, people running a
   server and contributors need different pages. The Federal Plain Language Guidelines warn that
   mixing audiences makes it "harder for each audience to find the material that applies to them".
   Three new files under `docs/` and one move into `deploy/README.md` absorb what the README drops.
3. **The shortest useful path needs the Copilot token; email is optional.** From `git clone` to
   scored stories takes 8 command lines and one admin form. Without `COPILOT_GITHUB_TOKEN`, Beehive
   still collects items but cannot score, summarize, deep read or research. Without Azure email
   settings it prints digests and alerts instead of sending them.

Recommendation: a README of about 100 lines with six sections, plus `docs/user-guide.md`,
`docs/configuration.md` and `docs/how-it-works.md`.

## What a README is for

| Source | The README should | Send elsewhere |
|---|---|---|
| [GitHub Docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes) | Say "what the project does", "why the project is useful", "how users can get started", "where users can get help" and "who maintains and contributes" | "A README should only contain information necessary for developers to get started using and contributing to your project. Longer documentation is best suited for wikis." |
| [Open Source Guides](https://opensource.guide/starting-a-project/#writing-a-readme) | Answer "What does this project do? Why is this project useful? How do I get started? Where can I get more help?" and explain "what your users can do with it"; say so if the project "is not yet ready for production" | Contribution rules, in a `CONTRIBUTING` file linked from the README |
| [Write the Docs](https://www.writethedocs.org/guide/writing/beginners-guide-to-docs/) | "clearly state what your project does and why"; "Keep your install instructions to a couple of lines for the basic case" | "Link to a page with more information and any caveats" |
| [Standard Readme](https://github.com/RichardLitt/standard-readme/blob/5d18ad4db6a39fde5dc845258828153eda9828e8/spec.md) | Title, optional banner and badges, a short description under 120 characters, then install and usage; a long description of "just a few paragraphs" | Deeper material "in subsequent sections". The spec "is designed for open source libraries", so its API rules do not fit Beehive |
| [Readme Driven Development](https://tom.preston-werner.com/2010/08/23/readme-driven-development.html) | Be "a single file that is intended to be read as an introduction to your software" | "lengthy or overprecise specification" |
| [Diátaxis](https://diataxis.fr/start-here/) | Defines no README type | Four kinds of documentation: tutorials, how-to guides, reference and explanation. "Crossing or blurring the boundaries described in the map is at the heart of a vast number of problems in documentation." |

Three more points shape the split:

- **Separate audiences.** "Mixing material intended for different audiences may confuse readers"
  ([Federal Plain Language Guidelines](https://github.com/GSA/plainlanguage.gov/blob/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines/audience/address-separate-audiences-separately.md)).
- **No empty structure.** Diátaxis: do not "create empty structures for tutorials/howto
  guides/reference/explanation with nothing in them" ([Workflow](https://diataxis.fr/how-to-use-diataxis/)).
- **Keep reference clean.** Reference should "describe and only describe", follow "the structure of
  the machinery", and never mix in "marketing claims" ([Reference](https://diataxis.fr/reference/)).

Diátaxis has no page on landing pages (its old `complex-hierarchies` URL returns 404), so treating
the README as the entry page that links to each kind of document is this note's inference.

Beehive has five kinds of reader:

| Reader | Needs | Home |
|---|---|---|
| Visitor | What it is, who it is for, what it looks like, what it needs (Copilot) | README |
| First-time user | The shortest path to real, scored stories | README quick start |
| Daily user (the owner) | How to set up channels, email, the Watch List and research | `docs/user-guide.md` |
| Server operator | Every setting and command; Podman units, secrets, backups, releases | `docs/configuration.md`, `deploy/README.md` |
| Contributor | Tests, lint, commits; how it works and why | `CONTRIBUTING.md`, `docs/how-it-works.md`, `docs/adr/`, `CONTEXT.md` |

## Structure: first screen, quick start, configuration and links

**What goes first.** Microsoft: "Content on the first screen (also called above the fold) is the
most likely to be read. Many readers won't scroll further without a compelling reason"
([Scannable content](https://learn.microsoft.com/en-us/style-guide/scannable-content/)). GOV.UK:
"Put the most important information first"; most people "only read 20 to 28% of text on a webpage"
([Create a clear structure](https://guidance.publishing.service.gov.uk/writing-to-gov-uk-standards/writing-guidelines/clear-structure/)).
Standard Readme fixes the order: title, banner, badges, short description, long description. Seven
of the ten projects below open with a sentence shaped like `Name is a kind of app …`, and six show
a screenshot within their first 25 source lines.

**Quick start length.** Write the Docs asks for "a couple of lines for the basic case" plus a link.
Google wants numbered steps, "one step for each action" and "Optional" on optional steps
([Procedures](https://developers.google.com/style/procedures)), and no "simply", "It's easy" or
"quickly" in a procedure ([Voice and tone](https://developers.google.com/style/tone)). Diátaxis wants
a learning exercise to be "meaningful" and "successful" ([Tutorials](https://diataxis.fr/tutorials/)),
so the reader must see a result. The comparable projects need 1 to 4 commands, and the best say where
the app is now running.

**Configuration.** Give the minimum inline and the full reference once, elsewhere. Glance keeps its
reference in `docs/configuration.md` with only a collapsed sample in the README; GitHub's collapsed
sections suit "technical details … that may not be relevant or interesting to every reader"
([Collapsed sections](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections)).
Write the Docs' "Unique" principle says "Eliminate content overlap between separate sources"
([Documentation principles](https://www.writethedocs.org/guide/writing/docs-principles/)); Beehive's
variable table is in the README, but `TRUSTED_PROXY_IPS` and `RESEARCH_WORKER_*` only in
`deploy/README.md`.

**Linking out.** Use relative links; GitHub warns that "Absolute links may not work in clones"
([About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#relative-links-and-image-paths-in-markdown-files)).
Link text should "surround words that describe the link itself" (Write the Docs principles). Miniflux
(a list of task links) and FreshRSS (user, administrator and developer docs) show the pattern.

## How comparable projects do it

Each README was read at a pinned commit on 2026-09-30. Line counts and words are from the raw file;
"top" is what the file shows before its first section.

| Project | First sentence | Top of README | Lines / words | Quick start in README | Deeper docs | Notably good / bad |
|---|---|---|---|---|---|---|
| [Miniflux](https://github.com/miniflux/v2/blob/4e6d7f93b2e9c036bd7a0b5299d0bd70f36718b4/README.md?plain=1#L4) | "Miniflux is a minimalist and opinionated feed reader." | Name, two sentences, website link. No logo or badges; the only screenshots come last ([L136](https://github.com/miniflux/v2/blob/4e6d7f93b2e9c036bd7a0b5299d0bd70f36718b4/README.md?plain=1#L136)) | 148 / 892 | None; links to install guide | miniflux.app/docs, from the separate `miniflux/website` repo | Good: a plain docs index of task links ([L115](https://github.com/miniflux/v2/blob/4e6d7f93b2e9c036bd7a0b5299d0bd70f36718b4/README.md?plain=1#L115)). Bad: about 100 lines of feature bullets before any picture |
| [FreshRSS](https://github.com/FreshRSS/FreshRSS/blob/0285dc8970246c95c2e588acd25ea2940899a5fd/README.md?plain=1#L8) | "FreshRSS is a self-hosted RSS feed aggregator." | Donation badge, then a note to read the file on GitHub "to get the correct links and pictures" ([L3](https://github.com/FreshRSS/FreshRSS/blob/0285dc8970246c95c2e588acd25ea2940899a5fd/README.md?plain=1#L3)); screenshot at L45 | 226 / 2,005 | Hosting buttons, or 7 numbered manual steps with no commands ([L95](https://github.com/FreshRSS/FreshRSS/blob/0285dc8970246c95c2e588acd25ea2940899a5fd/README.md?plain=1#L95)) | freshrss.github.io, built from in-repo `docs/` | Good: docs split into user, administrator and developer guides ([L51](https://github.com/FreshRSS/FreshRSS/blob/0285dc8970246c95c2e588acd25ea2940899a5fd/README.md?plain=1#L51)). Bad: relative links break off GitHub, hence the L3 note |
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io/blob/0e0566721b1c483dcf7ae548210ee10532d9b181/README.md?plain=1#L1) | After a search-engine headline: "Monitor websites for updates — get notified via Discord, Email, Slack, Telegram, Webhook and many more." | Screenshot at [L10](https://github.com/dgtlmoon/changedetection.io/blob/0e0566721b1c483dcf7ae548210ee10532d9b181/README.md?plain=1#L10), four badges, a hosted-plan pitch | 362 / 2,704 | `docker compose up -d` after cloning ([L158](https://github.com/dgtlmoon/changedetection.io/blob/0e0566721b1c483dcf7ae548210ee10532d9b181/README.md?plain=1#L158)), one `docker run`, or two `pip` commands | GitHub wiki | Good: screenshot first, and 25 use cases in plain words. Bad: notification, JSON and proxy reference sit in the README ([L214](https://github.com/dgtlmoon/changedetection.io/blob/0e0566721b1c483dcf7ae548210ee10532d9b181/README.md?plain=1#L214) onward) |
| [Huginn](https://github.com/huginn/huginn/blob/86ac2818e0dc4983aaecf73f126740071c57158e/README.md?plain=1#L7) | "Huginn is a system for building agents that perform automated tasks for you online." | Logo, "What is Huginn?", then things you can do ([L11](https://github.com/huginn/huginn/blob/86ac2818e0dc4983aaecf73f126740071c57158e/README.md?plain=1#L11)) | 156 / 1,220 | Docker: a link. Local: about 10 bullets that fork the repo and run `rake` ([L58](https://github.com/huginn/huginn/blob/86ac2818e0dc4983aaecf73f126740071c57158e/README.md?plain=1#L58)) | GitHub wiki and in-repo `doc/` | Good: outcomes first ("get an email when it's going to rain"). Bad: "Getting Started" is a contributor setup, with a "Develop" section ([L82](https://github.com/huginn/huginn/blob/86ac2818e0dc4983aaecf73f126740071c57158e/README.md?plain=1#L82)) |
| [Glance](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/README.md?plain=1#L15) | "A lightweight, highly customizable dashboard that displays your feeds in a beautiful, streamlined interface" | Logo, name, a row of links ([L4](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/README.md?plain=1#L4)), tagline, main screenshot (L17); no badges | 447 / 1,777 (16 collapsed `<details>`) | Fetch a template with one command, edit three files, `docker compose up -d` ([L185](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/README.md?plain=1#L185)) | In-repo `docs/*.md`, read on GitHub; no docs site | Good: the closest model for Beehive; the reference is `docs/configuration.md` and the README shows only a collapsed sample ([L59](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/README.md?plain=1#L59)). Bad: long in source though short when rendered |
| [Karakeep](https://github.com/karakeep-app/karakeep/blob/f908b02e904a030604d56d8bcd871763474006c9/README.md?plain=1#L18) | "Karakeep (previously Hoarder) is a self-hostable bookmark-everything app with a touch of AI for the data hoarders out there." | Four badges, logo, the sentence, screenshot (L20) | 124 / 943 | None; "Documentation" links Installation, Configuration, Security Considerations, Development ([L49](https://github.com/karakeep-app/karakeep/blob/f908b02e904a030604d56d8bcd871763474006c9/README.md?plain=1#L49)) | docs.karakeep.app, built from in-repo `docs/` | Good: a pure landing page; AI is one feature bullet. Bad: trying it always means leaving GitHub |
| [Linkwarden](https://github.com/linkwarden/linkwarden/blob/952ac4540657cae3a67c3ca59433899d2fda8374/README.md?plain=1#L30) | "Linkwarden is a self-hosted, open-source collaborative bookmark manager to collect, read, annotate, and fully preserve what matters, all in one place." | Logo, name, tagline "Bookmarks, Evolved", 7 badges, demo link, links row ([L22](https://github.com/linkwarden/linkwarden/blob/952ac4540657cae3a67c3ca59433899d2fda8374/README.md?plain=1#L22)), hero image | 152 / 952 | None; "Self-Host" links to the docs | docs.linkwarden.app, from the separate `linkwarden/docs` repo | Good: Cloud · Website · Self-Host · Docs row under the hero. Bad: badges and hero push the first sentence to line 30 |
| [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx/blob/4a106454ce2cd0e39896b2cb02d8432bc5d37cb5/README.md?plain=1#L20) | "Paperless-ngx is a document management system that transforms your physical documents into a searchable online archive so you can keep, well, _less paper_." | Six badges, logo with light and dark variants, the sentence, demo login, a contents list (L26) | 103 / 585 | One install-script command ([L63](https://github.com/paperless-ngx/paperless-ngx/blob/4a106454ce2cd0e39896b2cb02d8432bc5d37cb5/README.md?plain=1#L63)) | docs.paperless-ngx.com, built from in-repo `docs/` | Good: shortest README; features live in the docs (L54), and an "Important Note" states the security boundary ([L100](https://github.com/paperless-ngx/paperless-ngx/blob/4a106454ce2cd0e39896b2cb02d8432bc5d37cb5/README.md?plain=1#L100)). Bad: the one command pipes a remote script into `bash` |
| [Immich](https://github.com/immich-app/immich/blob/f77847dda0a72f138653b980a52ed9307372e02a/README.md?plain=1#L14) | Tagline only: "High performance self-hosted photo and video management solution" | Two badges, logo, tagline, large screenshot, 20 translated READMEs, a backup WARNING and a NOTE pointing to the docs ([L45](https://github.com/immich-app/immich/blob/f77847dda0a72f138653b980a52ed9307372e02a/README.md?plain=1#L45)) | 125 / 501 | None; a "Links" list (L53) | docs.immich.app, built from in-repo `docs/` | Good: two alerts carry the two things every user must know. Bad: no sentence says what it does beyond the tagline |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma/blob/e7420f8fa546a8baa7ac4bf9d10a32545d4346e0/README.md?plain=1#L7) | "Uptime Kuma is an easy-to-use self-hosted monitoring tool." | Logo, name, the sentence, 7 badges, screenshot (L14) | 205 / 901 | Docker Compose in 4 commands ([L38](https://github.com/louislam/uptime-kuma/blob/e7420f8fa546a8baa7ac4bf9d10a32545d4346e0/README.md?plain=1#L38)), or one `docker run` | GitHub wiki | Good: each path ends by saying it "is now running on all network interfaces (e.g. http://localhost:3001 …)" ([L49](https://github.com/louislam/uptime-kuma/blob/e7420f8fa546a8baa7ac4bf9d10a32545d4346e0/README.md?plain=1#L49)). Bad: 7 badges before the screenshot |

Patterns across the ten:

- **Opening.** Seven start with a sentence shaped like `Name is a kind of app …`, 8 to 25 words
  long; Glance and Immich use a tagline and changedetection.io a headline.
- **Picture early.** Six show a screenshot within their first 25 source lines.
- **Short or no install.** Five put no install commands in the README at all. The five that do keep
  the basic path to 1 to 4 commands, except Huginn, whose local path is a contributor setup.
- **Reference lives elsewhere.** None keeps a full configuration reference in the README. Four build
  a docs site from an in-repo `docs/` folder, Glance keeps plain `docs/*.md`, two use a separate docs
  repo, and three use the GitHub wiki.
- **Length.** 103 to 447 lines (median 154) and 501 to 2,704 words (median about 950). Beehive's
  README has 365 lines and 2,887 words, more words than any of them.

## Plain-language rules for Beehive

| # | Rule | Source | Beehive example |
|---|---|---|---|
| 1 | Lead with what the reader gets, then the detail. | Microsoft "[Get to the point fast](https://learn.microsoft.com/en-us/style-guide/top-10-tips-style-voice)"; GOV.UK "Frontload your content"; Federal guideline "[Place the main idea before exceptions and conditions](https://github.com/GSA/plainlanguage.gov/blob/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines/organize/place-the-main-idea-before-exceptions-and-conditions.md)" | Start the auction bullet with "Get an email an hour before a lot closes", not with how the worker runs |
| 2 | One idea per sentence; split any sentence over 25 words; aim for an average under 20. | GOV.UK "[Try to split up sentences that are over 25 words long](https://guidance.publishing.service.gov.uk/writing-to-gov-uk-standards/writing-guidelines/clear-language/)"; Federal "[Express only one idea in each sentence](https://github.com/GSA/plainlanguage.gov/blob/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines/concise/write-short-sentences.md)"; Google "[Write shorter sentences](https://developers.google.com/style/translation)" | Today 27% of README sentences exceed 25 words; the longest has 62 |
| 3 | Paragraphs of 3 to 5 sentences. | GOV.UK "no more than 5 sentences"; Microsoft "Three to seven lines" (Scannable content) | Split the 7-line research deletion bullet |
| 4 | Active voice, and talk to "you". | [Google](https://developers.google.com/style/voice); GOV.UK; Federal "[Address the user](https://github.com/GSA/plainlanguage.gov/blob/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines/audience/address-the-user.md)" | "Beehive emails you a digest", not "digests are delivered" |
| 5 | Simple words: "use", not "utilize"; "start", not "commence". | Microsoft "[Use simple words, concise sentences](https://learn.microsoft.com/en-us/style-guide/word-choice/use-simple-words-concise-sentences)"; Google global audience; GOV.UK words to avoid | "immutable" becomes "can't be changed later" |
| 6 | No internal names in the README. If a term is needed, say what it means the first time. | Google "[Jargon](https://developers.google.com/style/jargon)"; GOV.UK "Use specialist language if needed … explain what they mean the first time"; Federal "[Readers complain about jargon more than any other writing fault](https://github.com/GSA/plainlanguage.gov/blob/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines/words/avoid-jargon.md)" | Drop `RawItem`, "Research Plan", "Conversation Memory" |
| 7 | Use the words on screen, one term per concept, everywhere. | Microsoft "Use one term consistently to represent one concept"; Federal "[Use the same terms consistently](https://github.com/GSA/plainlanguage.gov/blob/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines/words/use-the-same-terms-consistently.md)"; GOV.UK "Use the language that users … use themselves" | "Channel type", not "Channel workflow" (see the term table) |
| 8 | Describe outcomes, not machinery. | Diátaxis: "How-to guides must be written from the perspective of the user, not of the machinery" ([How-to guides](https://diataxis.fr/how-to-guides/)); Huginn's "things you can do" list | Not "generic Tracker reminder worker" |
| 9 | Headings in sentence case, short and plain; start task headings with a verb; no questions; no unexplained terms. | [Google headings](https://developers.google.com/style/headings); Microsoft capitalization tip; GOV.UK headings | "Run it on a server", not "Deployment"; not "Research Sessions" |
| 10 | Numbered steps, one action each, say what you will see, mark optional steps. Never "simply" or "just". | Google procedures and tone | Quick start below |
| 11 | Give the minimum explanation inline and link to the rest. | Diátaxis: "the most minimal explanation … and then link to an in-depth article" ([Start here](https://diataxis.fr/start-here/)) | "Everything lives in one SQLite file" plus a link to `docs/how-it-works.md` |
| 12 | At most one or two alerts. | GitHub: "limit them to one or two per article" ([Alerts](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#alerts)) | One IMPORTANT alert about public reading pages |

## GitHub and packaging specifics

- **The first screen is the About box.** Measured on 2026-09-30, signed out, at 1440×900: the About
  box (description and 20 topics) starts at 206 px. Below the 21-row file list, the README starts at
  1,225 px, its hero fills 1,225 to 1,644 px, and its first sentence sits at 1,667 px. The README is
  11,506 px tall, about 13 screens. The About description is also the page title. Tabs for README,
  Contributing, MIT license and Security sit above the README, so footer links to those files serve
  clones, not GitHub (observed, not documented).
- **Three descriptions disagree.** GitHub About (182 characters), `pyproject.toml` `description`
  (98) and the README tagline (96) are different sentences. Standard Readme wants one short
  description under 120 characters, matching GitHub's and the package manager's. GOV.UK keeps
  summaries to 160 characters because "Google usually only shows the first 160 characters"
  ([Summaries](https://guidance.publishing.service.gov.uk/writing-to-gov-uk-standards/writing-guidelines/summaries/)).
- **Topics and social preview are done.** Beehive has 20 topics, GitHub's limit
  ([Topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics)),
  and a custom 1280×640 social preview, the size GitHub recommends
  ([Social preview](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview)).
  The same image is the README hero (`DESIGN.md:942`).
- **Outline and anchors.** GitHub builds a table of contents from headings, so headings are the
  navigation. Anchors come from heading text (lower case, spaces to hyphens, punctuation dropped;
  [Section links](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#section-links)),
  so renaming a heading breaks links to it: `CHANGELOG.md:22` points at `README.md#research-sessions`.
  Keeping `## Quick start` keeps `#quick-start`. With no text H1 today, the Outline starts at
  "Product tour".
- **Placement, links and rendering.** GitHub picks a README from `.github/`, the root or `docs/`, in
  that order, and truncates beyond 500 KiB (Beehive's is 20.3 KB); it rewrites relative links per
  branch and warns that absolute links "may not work in clones"
  ([About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)).
  Alerts, `<details>` and Mermaid all render
  ([Creating diagrams](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams)),
  so the pipeline diagram can move unchanged.
- **`docs/` over the wiki.** GitHub suggests wikis for longer documentation and calls the README the
  file that "quickly tells what your project can do"
  ([About wikis](https://docs.github.com/en/communities/documenting-your-project-with-wikis/about-wikis)).
  A wiki is a separate repository outside code review; Write the Docs' "Nearby" principle ("Store
  sources as close as possible to the code") favors `docs/`. Beehive's wiki was never created.
- **`pyproject.toml` `readme` matters for builds, not PyPI.** It is "a path relative to
  pyproject.toml to a text file containing the full description", copied into the `Description`
  metadata ([pyproject spec](https://packaging.python.org/en/latest/specifications/pyproject-toml/#readme)).
  Every build reads it, so `Containerfile:8` copies `README.md` before `pip install`. Keep the name
  and place, or change both files. Beehive is not published: CI only lints and tests, there are no
  tags or releases, and the PyPI name `beehive` belongs to an unrelated behavior-driven development
  tool. PyPI's rendering rules only matter if that changes.

## Assessment of Beehive

Measured on `README.md` at commit `124ab81`: 365 lines, 2,887 words, 6 images, 5 ADR references,
13 uses of "Owner" and 9 of "connector". Sentences average 19.7 words and 27% run over 25. The
intro's first sentence has 27 words, including "channel-specific interest profile" and
"conclusion-first summaries". Of the five sections in the links row, only "Quick start" survives.

| Current section (lines) | Verdict | Goes to | Reason |
|---|---|---|---|
| Opening: hero, tagline, links row, stats table, intro (1–29) | Rewrite | README | No text title. The stats table ("10 source families", "SQLite") sells internals, and "source families" is not a UI word. Keep the hero and the tagline's audience ("people who follow more sources than they have time to read"). |
| Product tour: four feature subsections (30–59) | Rewrite, move detail | README "What Beehive does"; detail to `docs/user-guide.md` | Opens with design vocabulary from `DESIGN.md` ("contents rail of numbered chapters", "banded tables") instead of outcomes. The Auckland window, brief length and wide-screen layout are user-guide detail. |
| Use the right workflow for each Channel (61–75) | Move | `docs/user-guide.md` (types table); `docs/how-it-works.md` (compatibility rules) | "Workflow", "immutable" and "Panel behavior" are internal. The README needs one bullet per channel type. |
| How it works (77–95) | Move | `docs/how-it-works.md` | Architecture explanation naming `RawItem`. |
| Supported sources (97–110) | Shorten | README names sources inside the feature bullets; table to `docs/user-guide.md` | Visitors need the list to decide. The "Integration" column is reference. |
| Research Sessions (112–188) | Move | `docs/user-guide.md` (how to use, limits); `docs/how-it-works.md` (trust model, stored text, deletion, ADR-0006 to ADR-0010) | 796 words, 28% of the README, for a feature only the signed-in owner can see. Uses Research Plan, Research Run, Evidence Item, Conversation Memory. One README bullet is enough. |
| Quick start (190–211) | Rewrite | README | Runs `pytest`, which `CONTRIBUTING.md` already covers. Never sets the token or fetches, so it ends with an empty instance. The requirements list is good. |
| Configuration: variables (213–225) | Move | `docs/configuration.md`; the quick start keeps the three variables it needs | Reference, and incomplete (`TRUSTED_PROXY_IPS`, `RESEARCH_WORKER_*`). |
| Configuration: language and model (227–233) | Move | `docs/configuration.md` "Admin settings" | Reference. The README keeps "7 languages" as a feature. |
| Admin operations (235–254) | Move | `docs/user-guide.md` (undo, health, test samples, email preview); `docs/configuration.md` (schedule rules) | How-to and reference for existing users. |
| Collect and digest (256–296) | Move | `docs/configuration.md` "Commands"; the quick start keeps one fetch command | Reference, with a compatibility note about the `auction-reminders` mode name. |
| Rewrite existing unread summaries (298–338) | Move | `deploy/README.md` "One-time upgrade tasks" | A one-time migration for databases from before one-sentence summaries. New users never need it. Delete it once no such database remains. |
| Deployment (339–344) | Keep, shorten | README "Run it on a server" | The pointer is right; the unit list repeats the `deploy/README.md` "Files" table. |
| Privacy and indexing (346–355) | Rewrite short | README alert; detail to `docs/how-it-works.md` | "Reading pages are public" changes whether you expose Beehive. Headers and link-preview tags are detail. |
| Project status (357–365) | Keep | README | Open Source Guides: if a project "is not yet ready for production, write this information down". |

## Recommended Beehive structure

### README outline

About 100 lines and under 1,000 words. Headings in order:

| # | Heading | What it says | Lines |
|---|---|---|---|
| 1 | `# Beehive` | Text title; the hero image with its current alt text; the two-sentence opening below; one facts line; a links row to What it does · Quick start · Documentation · Run it on a server | 12 |
| 2 | `## What Beehive does` | Eight outcome bullets, each starting with a verb and naming the on-screen feature in bold; two screenshots (`dashboard-product.png`, `channel-monitor.png`); the note that previews use synthetic data | 26 |
| 3 | `## Quick start` | "What you need" (three bullets: Python 3.12 and git; a GitHub Copilot token, required; Azure email, optional); five numbered steps with 8 command lines; what you should see; three "Optional" lines (deep reads, research, email) linking to `docs/configuration.md` | 40 |
| 4 | `## Run it on a server` | Two sentences: rootless Podman units run fetches, digests, reminders, deep reads and research on timers; link to `deploy/README.md`. One IMPORTANT alert: anyone who can reach Beehive can read its home, channel and archive pages; admin, Research, Deep read and the Watch List need the password (ADR-0003; `src/beehive/web/public.py:838,1454`) | 8 |
| 5 | `## Documentation` | One line each: User guide, Configuration, How it works, Deployment, Contributing, Changelog, Security, Design decisions (`docs/adr/`), Glossary (`CONTEXT.md`) | 12 |
| 6 | `## Project status` | 0.1.0 alpha, used daily by its maintainer; database upgrades are not yet guaranteed | 3 |
| 7 | `## License` | MIT, with a relative link | 2 |

**First screen.** At 1440×900 the README's first screen should show the text title, the hero (about
420 px), the opening, the facts line, the links row and the first feature bullets, with the home
page screenshot in the second screen. The GitHub About description should repeat the opening.

**Draft opening** (21 and 24 words):

> Beehive is a self-hosted app for people who follow more news, online stores and auctions than
> they have time to read. Tell it what you care about, and its AI picks the stories, sales and
> auction lots that match, with a one-line summary of each.

Facts line: "Read them on one page or in email digests on your schedule. Beehive runs on your own server for
one person and uses your GitHub Copilot subscription for the AI."

For the GitHub About box and `pyproject.toml` `description` (115 characters): "A self-hosted app
that follows news, online stores and auctions for you, and uses AI to show you only what matters."

**Draft feature bullets** (facts checked against the current README, CHANGELOG and UI strings):

- **Follow the news you care about.** Stories from Reddit, Google News searches, Hacker News and the
  official feeds of the Reserve Bank of New Zealand, the NZ Government and the US Federal Reserve.
- **See the best first.** Each new story gets a 0 to 100 score against your description; the home
  page shows each channel's top stories with a one-line summary.
- **Catch sales and restocks.** A store monitor (type **Monitor**) watches Shopify stores, Land & Sea,
  THE OUTNET, Mytheresa, END. and YOOX for new items, price drops and restocks.
- **Never miss an auction.** An auction tracker (type **Tracker**) follows All About Auctions lots.
  Add a lot to your **Watch List** and Beehive emails you about an hour before it closes.
- **Read long articles in brief.** **Deep read** writes a short brief of the full article: the
  bottom line, key findings and important figures.
- **Get digests on your schedule.** Put channels in email groups, each with its own schedule.
- **Research a one-off question.** A research session gathers evidence from the same news sources
  and writes an answer with numbered sources.
- **Use your language.** Pages, emails and AI summaries in English, Simplified Chinese, Japanese,
  Korean, Spanish, French or German.

### Docs split

Three new files, each with enough content to stand alone, plus moves into existing files.

| File | Reader and kind | Moves in from `README.md` | Budget |
|---|---|---|---|
| `docs/user-guide.md` (new) | The owner using Beehive. Task-based how-to with short explanations, headed by what you want to do: Create a channel; Choose a channel type; Add sources; Read your highlights; Get a deep read; Watch an auction lot; Set up email digests; Research a question; Undo a change; Check that everything works | 35–59 (featured window, counts, wide screens, deep read details, controls, plus the `wide-screen.png` and `channel-configuration.png` screenshots); 61–69 (channel types table, reworded with UI labels); 97–110 (sources table); 112–153 and the user-facing half of 155–182 (what Research does, what it can't do, archive versus delete); 235–242 (activity, health, test samples, 7-day undo); 249–254 (email groups, preview, test send, repeat suppression); 270–272 (reminder timing); 278–280 | ~160 |
| `docs/configuration.md` (new) | Anyone running Beehive. Reference for every setting, variable and command | 213–225 (variables table, plus `TRUSTED_PROXY_IPS` and a pointer to `RESEARCH_WORKER_*`); 227–233 (language and model); 244–247 and 249–251 (schedule rules); 256–268, 274–276 and 282–296 (collector modes, digest, reminders, deep-read and Research workers). New: an "Admin settings" list in UI words, and "Running without the server setup" (below) | ~120 |
| `docs/how-it-works.md` (new) | Curious users and contributors. Explanation | 77–95 (diagram and pipeline); 71–73 (source compatibility); the design half of 155–188 (tool-free AI calls, labeled model knowledge, stored text, deletion limits, ADR-0006 to ADR-0010, `src/beehive/research/`); 346–355 (search indexing, link previews, public pages). Links to `CONTEXT.md` and `docs/adr/` | ~90 |
| `deploy/README.md` (existing) | Server operator | 298–338 as a new "One-time upgrade tasks" section. The unit list at 339–344 is already its "Files" table | +45 |
| `CONTRIBUTING.md` (existing) | Contributors | Nothing new to move: `pytest` (README 201) is already under "Running tests". Add one line linking `docs/how-it-works.md`, `docs/adr/` and `CONTEXT.md` | +2 |
| `CHANGELOG.md` (existing) | | Point line 22 at the new user-guide anchor instead of `README.md#research-sessions` | 0 |

Keep `PRODUCT.md`, `DESIGN.md` and `CONTEXT.md` where they are: they are design and tool context, not
user docs. Do not add `docs/README.md`; the README's Documentation section is the index.

### Plain words for internal terms

The right-hand columns are the English UI strings in `src/beehive/translations/`. When the README
tells you what to click, it uses the on-screen label in bold; in running prose it uses the plain
phrase.

| In the current README | Say in the README | On screen (English) | Key |
|---|---|---|---|
| Channel workflow; workflow | channel type | Channel type | `web.admin.channel_new.kind_label` |
| Editorial (workflow, Channel) | news channel; type **Editorial** | Editorial | `web.channel.editorial_label` |
| Monitor (workflow, Channel) | store monitor; type **Monitor** | Monitor | `web.channel.monitor_label` |
| Tracker (workflow, Channel) | auction tracker; type **Tracker** | Tracker | `web.channel.tracker_label` |
| Owner; Owner session; owner-only | you; "after you sign in" | Sign in to admin; Owner workspace | `web.admin.login.heading`; `web.research.list.eyebrow` |
| source families; connector; adapter | source; kind of source | Add source; source types | `web.admin.channel_edit.add_source`; `web.admin.source_form.types_note` |
| channel-specific interest profile | what you care about | What to focus on | `web.admin.channel_new.profile_label` |
| conclusion-first summary | one-line summary | Summary | `web.reading.col_summary` |
| Deep Read; AI brief; article brief | **Deep read**: a short AI brief of the full article | Deep read; Open brief | `web.deep_read.button_start`; `web.deep_read.button_open` |
| personal dashboard; Dashboard | home page | Home (menu); Featured (page heading) | `web.nav.home`; `web.dashboard.heading` |
| Panels; dedicated panels | channel page | Open channel page | `web.admin.channel_edit.open_channel` |
| Email Group | email group | Email groups | `web.admin.chapter.groups` |
| watchable Tracker items; Tracker Watch | watch a lot; Watch List | Watch; Watch List | `web.tracker.watch`; `web.nav.watchlist` |
| generic Tracker reminder worker | an email about an hour before a watched lot closes | Tracker reminders | `web.watchlist.eyebrow`; `web.workspace.watch.reminder_rule` |
| Research Session | research session | New Research Session | `web.research.list.new_session` |
| Research Question | your question | Research Question | `web.research.new.question_label` |
| Research Plan | the searches Beehive plans to run | Sources and plan | `web.workspace.session.title_plan` |
| Research Run | each time it gathers evidence | Run history; Refresh research | `web.workspace.session.title_history`; `web.workspace.session.refresh` |
| Evidence Item; Evidence Snapshot | evidence; the sources it found | Evidence | `web.workspace.session.title_evidence` |
| Research Synthesis; stable citations | the answer, with numbered sources | Conclusion | `web.workspace.session.synthesis_who` |
| Conversation Memory | leave out | not shown | `CONTEXT.md` |
| global LLM model | AI model | LLM model | `web.admin.model.heading` |
| global platform language | language | Platform language | `web.admin.language.heading` |
| highlights; minimum visible AI score | top stories; minimum score | Highlights per channel; Minimum AI score | `web.admin.channel_new.highlight_count_label`; `web.admin.channel_new.minimum_score_label` |
| cadence; fetch schedule | how often it checks | Fetch schedule (its interval drop-down is Fetch cadence) | `web.admin.channel_schedule.heading`; `web.admin.channel_new.interval_label` |
| Auckland calendar-day window | the last three days (you can change it) | Featured window | `web.admin.featured.heading` |
| System Health | the System page | System | `web.admin.chapter.system` |
| `RawItem`; ADR numbers; Quadlet; `X-Robots-Tag` | leave out of the README | not shown | |

"Store" and "shop" each appear once in the UI (`web.admin.source_new.shopify_collection_url_hint`,
`web.admin.source_type_hint.shopify_collection`); this note uses "store", to match "store monitor".

Three UI strings disagree with the product; fix them separately, before the README repeats them.
The **Monitor** and **Tracker** type hints (`src/beehive/translations/web.py:346,359`) say items
appear "not on Home or in the daily digest", yet the home page shows live listings and open lots
(`web_reading.py:27-36`, `README.md:37`) and email groups may mix channel types (`README.md:93`,
`CHANGELOG.md:55`); the owner should confirm. "Dashboard" survives in
`web.deep_read.back_to_dashboard` and `deploy/README.md:6` while the menu says "Home". Several hints
say "daily digest", though email groups run on any schedule.

### Shortest path from clone to a useful instance

Checked against `pyproject.toml`, `scripts/`, `src/` and `deploy/README.md`. Nothing was run.

**What you need:** Python 3.12 (`pyproject.toml:7`) and git on Linux (CI and the container use
Linux; macOS and Windows are not confirmed). **Required:** a GitHub account with a Copilot
subscription and a fine-grained personal access token, owned by your personal account, with the
"Copilot Requests" permission; classic `ghp_` tokens are not supported
([Authenticating Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli);
[Copilot SDK authentication](https://github.com/github/copilot-sdk/blob/a2b2c18eb5a20417fc613eaaa93199f55ad22ea4/docs/auth/authenticate.md)).
**Optional:** Azure Communication Services, the only email provider; there is no SMTP
(`src/beehive/notify.py:51-59`).

1. Get the code and install it. The `ai` extra brings the Copilot SDK; add `email` only for Azure.

   ```bash
   git clone https://github.com/sinmentis/beehive.git && cd beehive
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -e ".[ai]"
   ```

2. Choose where the database lives, create a session secret, and set the admin password.

   ```bash
   export DB_PATH="$PWD/beehive.db" SESSION_SECRET="$(openssl rand -hex 32)"
   .venv/bin/python -m scripts.set_admin_password
   ```

   `DB_PATH` defaults to the container's `/data/beehive.db`, and every script reads it
   (`scripts/run_web.py:13`, `scripts/set_admin_password.py:31`). The web app refuses a secret under
   32 characters (`src/beehive/web/app.py:38,51-57`), and nobody can sign in until a password is set
   (`src/beehive/web/admin.py:309-310`).

3. Start the web app: `.venv/bin/python -m scripts.run_web`. It listens on port 8000 on all network
   interfaces (`scripts/run_web.py:19`); the README should say so, as Uptime Kuma does.
4. Open `http://127.0.0.1:8000/admin` and sign in. Select **New channel**, pick **Editorial**, fill
   in **What to focus on**, create it, then **Add source** (for example a Google News search).
5. In a second terminal, fetch and score:

   ```bash
   cd beehive && export DB_PATH="$PWD/beehive.db" COPILOT_GITHUB_TOKEN="github_pat_..."
   .venv/bin/python -m scripts.run_collector --mode fetch
   ```

   New sources are due at once (`src/beehive/scheduling.py:240-248`). Reload
   `http://127.0.0.1:8000/`: the channel's top stories should appear with scores and summaries.

**Token.** Say it is required for scores, summaries, Deep read, Research and the model list. Only the
collector and the deep-read and Research workers use it, not the web app (`deploy/README.md:190`).
Without it Beehive still collects items, but each fetch reports an AI failure and nothing is scored
(`src/beehive/collector/run_cycle.py:250-277`), and Deep read refuses unscored items
(`src/beehive/web/public.py:1476-1477`). The SDK would also fall back to a Copilot CLI or `gh` login
because Beehive calls `CopilotClient()` with no arguments (`src/beehive/ai/llm_client.py:99`); that
is untested, so the README should just ask for the variable.

**Email.** Say it is optional. Without `ACS_CONNECTION_STRING`, digests, alerts and reminders are
printed instead of sent (`src/beehive/notify.py:51-54`). To send, install the `email` extra and set
`ACS_CONNECTION_STRING` and `DIGEST_EMAIL_FROM` (a sender your Azure resource may use; the default is
`beehive@example.com`). Set the recipient with **Default email address** at the top of **Email groups**, or
with `DIGEST_EMAIL_TO`; the admin value wins (`src/beehive/email_routing.py:54-61`).

**Running without the server setup** (for `docs/configuration.md`; one line in the README).
**Fetch now** only queues work: on a server the `beehive-fetch-manual.path` unit renames the trigger
file that `--mode fetch-channel` reads (`src/beehive/collector/manual_trigger.py:1-9`), so locally
run `--mode fetch`. Deep reads wait for `--mode deep-read`, Research and **Refresh list** for
`scripts.run_research_worker`, digests for `--mode digest`, reminders for `--mode tracker-reminders`.
The server setup runs all of these on timers or as services (`deploy/README.md`, "Files").

**Not confirmed:** macOS and Windows; which Copilot plans work (the SDK only marks token sign-in as
needing a Copilot subscription); network access for the Copilot runtime, which the SDK downloads "on
first managed stdio/TCP use" unless fetched ahead ([SDK Python README](https://github.com/github/copilot-sdk/blob/a2b2c18eb5a20417fc613eaaa93199f55ad22ea4/python/README.md));
and that stories show on the home page after the first fetch (code read, not run).

### Checks the new README should pass

1. The first two sentences say what Beehive is, who it is for and what it does, in 25 words or fewer
   each, with no term from the "leave out" rows above.
2. The About box, `pyproject.toml` `description` and the README opening say one thing; the first two
   are under 120 characters.
3. At 1440×900 the text title, hero and opening fit in the README's first 900 px (today the first
   sentence is 442 px in), and a real home page screenshot appears within two screens.
4. At most 120 source lines, 1,000 words and 4 rendered screens (today 365, 2,887 and about 13).
5. Quick start: at most 8 command lines to scored stories; no test or lint commands; numbered steps;
   the URL to open and what you should see; optional steps marked "Optional".
6. The README names only `DB_PATH`, `SESSION_SECRET` and `COPILOT_GITHUB_TOKEN`; every variable read
   in `src/` and `scripts/` (`grep -rn "os.environ" src scripts`) is in `docs/configuration.md`.
7. `README.md` contains none of `RawItem`, `ADR-`, `connector`, `adapter`, `workflow`,
   `Evidence Item`, `Research Plan`, `Conversation Memory` or a capitalized `Owner`, and every UI
   name in it matches an English string in `src/beehive/translations/`.
8. Sentences average 20 words or fewer, with at most 10% over 25 (today 19.7 and 27%).
9. At most three images, each with alt text; one GitHub alert at most; relative links only, none
   broken, including anchors linked from `CHANGELOG.md`.
10. Headings in sentence case, five words or fewer, no questions and no internal terms.

### Suggested order of work

Diátaxis advises working "one step at a time", so move first and improve after: create the three
`docs/` files by moving text as it is and fix `CHANGELOG.md:22`; move the summary rewrite into
`deploy/README.md`; rewrite the README to the outline and term table; update the About description
and `pyproject.toml`; then run the checks.

## Primary sources

- GitHub Docs: [About the repository README file](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes), [Basic writing and formatting syntax](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax), [Collapsed sections](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections), [Creating diagrams](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams), [About wikis](https://docs.github.com/en/communities/documenting-your-project-with-wikis/about-wikis)
- GitHub Docs: [Classifying your repository with topics](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics), [Customizing your repository's social media preview](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview), [Authenticating GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli)
- [GitHub Copilot SDK: Authentication](https://github.com/github/copilot-sdk/blob/a2b2c18eb5a20417fc613eaaa93199f55ad22ea4/docs/auth/authenticate.md) and [Python README](https://github.com/github/copilot-sdk/blob/a2b2c18eb5a20417fc613eaaa93199f55ad22ea4/python/README.md)
- [Diátaxis: Start here](https://diataxis.fr/start-here/), [Workflow](https://diataxis.fr/how-to-use-diataxis/), [Tutorials](https://diataxis.fr/tutorials/), [How-to guides](https://diataxis.fr/how-to-guides/), [Reference](https://diataxis.fr/reference/), [Explanation](https://diataxis.fr/explanation/)
- [Write the Docs: How to write software documentation](https://www.writethedocs.org/guide/writing/beginners-guide-to-docs/)
- [Write the Docs: Documentation principles](https://www.writethedocs.org/guide/writing/docs-principles/)
- [Open Source Guides: Starting an Open Source Project](https://opensource.guide/starting-a-project/)
- [Standard Readme specification](https://github.com/RichardLitt/standard-readme/blob/5d18ad4db6a39fde5dc845258828153eda9828e8/spec.md)
- [Tom Preston-Werner: Readme Driven Development](https://tom.preston-werner.com/2010/08/23/readme-driven-development.html)
- [Python Packaging User Guide: Writing your pyproject.toml, `readme`](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#readme)
- [PyPA specification: pyproject.toml, `readme`](https://packaging.python.org/en/latest/specifications/pyproject-toml/#readme)
- [Python Packaging User Guide: Making a PyPI-friendly README](https://packaging.python.org/en/latest/guides/making-a-pypi-friendly-readme/)
- [Google developer documentation style guide: Voice and tone](https://developers.google.com/style/tone), [Active voice](https://developers.google.com/style/voice), [Jargon](https://developers.google.com/style/jargon), [Writing for a global audience](https://developers.google.com/style/translation), [Headings](https://developers.google.com/style/headings), [Procedures](https://developers.google.com/style/procedures)
- [Microsoft Writing Style Guide: Top 10 tips](https://learn.microsoft.com/en-us/style-guide/top-10-tips-style-voice), [Use simple words, concise sentences](https://learn.microsoft.com/en-us/style-guide/word-choice/use-simple-words-concise-sentences), [Scannable content](https://learn.microsoft.com/en-us/style-guide/scannable-content/)
- GOV.UK writing guidelines (moved from gov.uk/guidance/content-design): [Use clear language](https://guidance.publishing.service.gov.uk/writing-to-gov-uk-standards/writing-guidelines/clear-language/), [Create a clear structure](https://guidance.publishing.service.gov.uk/writing-to-gov-uk-standards/writing-guidelines/clear-structure/), [Write clear summaries](https://guidance.publishing.service.gov.uk/writing-to-gov-uk-standards/writing-guidelines/summaries/)
- Plain language: plainlanguage.gov now redirects to [digital.gov's plain language guides](https://digital.gov/guides/plain-language/); the original Federal Plain Language Guidelines are archived in [GSA/plainlanguage.gov](https://github.com/GSA/plainlanguage.gov/tree/fd7694740f19c0ed20ed71c2dd1dc92699e920fa/_pages/guidelines)
- Example READMEs at the commits read: [Miniflux](https://github.com/miniflux/v2/blob/4e6d7f93b2e9c036bd7a0b5299d0bd70f36718b4/README.md), [FreshRSS](https://github.com/FreshRSS/FreshRSS/blob/0285dc8970246c95c2e588acd25ea2940899a5fd/README.md), [changedetection.io](https://github.com/dgtlmoon/changedetection.io/blob/0e0566721b1c483dcf7ae548210ee10532d9b181/README.md), [Huginn](https://github.com/huginn/huginn/blob/86ac2818e0dc4983aaecf73f126740071c57158e/README.md), [Glance](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/README.md), [Karakeep](https://github.com/karakeep-app/karakeep/blob/f908b02e904a030604d56d8bcd871763474006c9/README.md), [Linkwarden](https://github.com/linkwarden/linkwarden/blob/952ac4540657cae3a67c3ca59433899d2fda8374/README.md), [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx/blob/4a106454ce2cd0e39896b2cb02d8432bc5d37cb5/README.md), [Immich](https://github.com/immich-app/immich/blob/f77847dda0a72f138653b980a52ed9307372e02a/README.md), [Uptime Kuma](https://github.com/louislam/uptime-kuma/blob/e7420f8fa546a8baa7ac4bf9d10a32545d4346e0/README.md)
