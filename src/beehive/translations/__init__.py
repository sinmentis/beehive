"""Built-in translation catalogs grouped by rendering domain. MODULES is every catalog module the
localizer loads; no key may appear in more than one of them."""
from beehive.translations import background, common, web, web_admin, web_reading, web_workspace

MODULES = (common, web, web_admin, web_reading, web_workspace, background)
