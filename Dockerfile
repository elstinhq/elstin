# The UI is built here, in its own stage, and copied into the runtime
# image. That is possible because every frontend dependency is public
# (react, react-dom, @tanstack/react-query, Radix UI, Tailwind,
# lucide-react, sonner, esbuild, typescript — shadcn/ui vendored as
# source, not pulled from its CLI's registry): the UI previously depended
# on a design system published only to a private registry, which meant
# the image could not build its own frontend and had to ship whatever a
# developer happened to have built on their host — an image built from a
# fresh clone served JS-less HTML. See ui-src/README.md.
FROM node:22-slim AS ui-build
WORKDIR /ui
COPY ui-src/package.json ui-src/package-lock.json ./
RUN npm ci
COPY ui-src ./
COPY settings-ui ../settings-ui
RUN npm run build

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY forge ./forge
COPY alembic.ini ./
COPY alembic ./alembic
COPY settings-ui ./settings-ui
# The bundle + stylesheet the stage above produced (settings-ui/vendor/ is
# gitignored, so it is never copied in from the build context).
COPY --from=ui-build /settings-ui/vendor ./settings-ui/vendor

RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "forge.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
