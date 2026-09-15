# Dedinsky for Judge

Campaign website for Paul Dedinsky, candidate for Waukesha County Circuit Court Judge (Wisconsin Spring Election, April 6, 2027).

## About

Static campaign site: plain HTML, CSS, and vanilla JS. No build step, no framework, no third-party requests at runtime — fonts and the Formspree SDK are self-hosted.

## Structure

```
├── index.html              Landing page
├── about.html              Bio, career, education, community involvement
├── vote.html               Election info, deadlines, judicial philosophy
├── endorsements.html       Endorsements
├── support.html            Volunteer form (Formspree) and ways to help
├── donate.html             Contribution page (links out to WinRed)
├── 404.html                Custom not-found page
├── _headers                Security + cache headers (Netlify)
├── _redirects              Keeps _includes/ from being served as pages
├── _includes/
│   ├── nav.html            Shared nav, stamped into every page
│   ├── footer.html         Shared footer, stamped into every page
│   └── render.py           Stamps the partials into the pages (see below)
├── robots.txt / sitemap.xml / llms.txt / site.webmanifest
├── .well-known/security.txt
├── css/
│   ├── style.css           Shared styles and responsive breakpoints
│   └── fonts.css           @font-face for self-hosted variable fonts
├── js/
│   ├── nav.js              Mobile menu toggle + shrinking nav
│   ├── formspree-init.js   Form config (form ID, styling opts)
│   └── formspree-ajax-*.js Self-hosted Formspree SDK
├── fonts/                  Cormorant Garamond + Work Sans (woff2, variable)
└── img/                    Photos, logo, icons
```

## Shared nav and footer

There is no runtime templating, so the nav and footer are copied into each page between marker comments (`<!-- include:nav -->` … `<!-- /include:nav -->`). Edit `_includes/nav.html` or `_includes/footer.html`, then stamp them into every page:

```
python3 _includes/render.py          # rewrite pages
python3 _includes/render.py --check  # exit 1 if any page is stale
```

The render marks the current page's nav link active. Never edit the nav or footer inside a page directly; the next render overwrites it.
Both modes reject malformed markers and missing, duplicate, or misordered shared structures before changing any page.

## Deployment

Hosted on [Netlify](https://netlify.com) (Pro). Pushing to `main` auto-deploys to `dedinsky4judge.com`. Headers and caching are configured in `_headers`; pages are served at clean extensionless URLs.

## Local Preview

Asset paths are root-absolute, so preview through a local server (not `file://`):

```
python3 -m http.server 8000
# then open http://localhost:8000
```

## License

Content and images are property of the Dedinsky for Judge campaign committee. Not licensed for reuse.
