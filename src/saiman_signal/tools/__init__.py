from saiman_signal.tools.beli_lookup import DEFINITION as BELI_LOOKUP_DEF
from saiman_signal.tools.beli_lookup import execute as beli_lookup
from saiman_signal.tools.get_page_contents import DEFINITION as GET_PAGE_CONTENTS_DEF
from saiman_signal.tools.get_page_contents import execute as get_page_contents
from saiman_signal.tools.reddit_read import DEFINITION as REDDIT_READ_DEF
from saiman_signal.tools.reddit_read import execute as reddit_read
from saiman_signal.tools.reddit_search import DEFINITION as REDDIT_SEARCH_DEF
from saiman_signal.tools.reddit_search import execute as reddit_search
from saiman_signal.tools.set_location import DEFINITION as SET_LOCATION_DEF
from saiman_signal.tools.set_location import execute as set_location
from saiman_signal.tools.web_search import DEFINITION as WEB_SEARCH_DEF
from saiman_signal.tools.web_search import execute as web_search

TOOL_DEFINITIONS = [
    WEB_SEARCH_DEF,
    GET_PAGE_CONTENTS_DEF,
    REDDIT_SEARCH_DEF,
    REDDIT_READ_DEF,
    BELI_LOOKUP_DEF,
    SET_LOCATION_DEF,
]

# Apply cache_control to last tool definition for stable caching
TOOL_DEFINITIONS[-1] = {**TOOL_DEFINITIONS[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}

TOOLS = {
    "web_search": web_search,
    "get_page_contents": get_page_contents,
    "reddit_search": reddit_search,
    "reddit_read": reddit_read,
    "beli_lookup": beli_lookup,
    "set_location": set_location,
}
