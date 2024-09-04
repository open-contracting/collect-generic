from urllib.parse import parse_qs, urlencode, urlsplit


def replace_parameters(url, **kwargs):
    """
    Return a URL after updating the query string parameters' values.
    """
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    for key, value in kwargs.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = [value]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()
