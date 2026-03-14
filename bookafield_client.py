import json
import logging
import os
import re
import uuid
import pandas as pd
import yaml
from http.cookiejar import CookieJar
from typing import Dict, Optional, Tuple
from urllib import error, parse, request
class BookaFieldError(Exception):
    pass
class ConfigurationError(BookaFieldError):
    pass
class AuthenticationError(BookaFieldError):
    pass
class APIError(BookaFieldError):
    pass
class APIResponse:
    def __init__(self, status_code: int, body: Dict[str, object]):
        self.status_code = status_code
        self.body = body
class BookaFieldClient:
    def __init__(self, config: Dict[str, object], dry_run: bool = False, timeout_seconds: int = 30) -> None:
        self._config = config
        self._dry_run = dry_run
        self._timeout_seconds = timeout_seconds
        self._csrf_token: Optional[str] = None
        self._cookie_jar = CookieJar()
        self._opener = request.build_opener(request.HTTPCookieProcessor(self._cookie_jar))
        self._resource_mapping = self._load_resource_mapping()
        self.authenticate()
    def _load_resource_mapping(self):
        try:
            return pd.read_csv("docs/resource_mapping_example.csv", index_col="resource_name")
        except FileNotFoundError:
            raise ConfigurationError("Resource mapping file not found at docs/resource_mapping_example.csv")
    def _resolve_value(self, value: object) -> str:
        raw = str(value or "").strip()
        if raw.startswith("${") and raw.endswith("}"):
            env_name = raw[2:-1].strip()
            return os.environ.get(env_name, "").strip()
        return raw
    def _compose_url(self, endpoint: str) -> str:
        base_url = self._resolve_value(self._config.get("base_url")).rstrip("/")
        if not base_url:
            raise ConfigurationError("Missing config value: base_url")
        endpoint = str(endpoint or "").strip()
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        if not endpoint:
            raise ConfigurationError("Missing API endpoint path")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return f"{base_url}{endpoint}"
    def _encode_multipart(self, payload: Dict[str, object], boundary: str) -> bytes:
        lines = []
        for key, value in payload.items():
            lines.append(f"--{boundary}")
            lines.append(f'Content-Disposition: form-data; name="{key}"')
            lines.append("")
            lines.append(str(value if value is not None else ""))
        lines.append(f"--{boundary}--")
        lines.append("")
        return "\r\n".join(lines).encode("utf-8")
    def _request(
        self,
        method: str,
        endpoint: str,
        payload: Optional[Dict[str, object]] = None,
        headers: Optional[Dict[str, str]] = None,
        payload_type: str = "json",
    ) -> APIResponse:
        url = self._compose_url(endpoint)
        request_headers: Dict[str, str] = {"Accept": "*/*"}
        body_bytes: Optional[bytes] = None
        if payload is not None:
            if payload_type == "form":
                request_headers["Content-Type"] = "application/x-www-form-urlencoded"
                body_bytes = parse.urlencode({k: "" if v is None else v for k, v in payload.items()}).encode("utf-8")
            elif payload_type == "multipart":
                boundary = f"----CodexBoundary{uuid.uuid4().hex}"
                request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
                body_bytes = self._encode_multipart(payload, boundary)
            else:
                request_headers["Content-Type"] = "application/json"
                body_bytes = json.dumps(payload).encode("utf-8")
        if headers:
            request_headers.update(headers)
        req = request.Request(
            url,
            data=body_bytes,
            method=method.upper(),
            headers=request_headers,
        )
        try:
            with self._opener.open(req, timeout=self._timeout_seconds) as response:
                response_bytes = response.read() or b""
                response_text = response_bytes.decode("utf-8", errors="replace") if response_bytes else ""
                body: Dict[str, object]
                if response_text:
                    try:
                        parsed = json.loads(response_text)
                        body = parsed if isinstance(parsed, dict) else {"parsed": parsed}
                    except json.JSONDecodeError:
                        body = {"raw_text": response_text}
                else:
                    body = {}
                body["_request_url"] = url
                body["_response_url"] = response.geturl()
                body["_request_method"] = method.upper()
                return APIResponse(status_code=response.status, body=body)
        except error.HTTPError as exc:
            response_bytes = exc.read() or b""
            response_text = response_bytes.decode("utf-8", errors="replace") if response_bytes else ""
            snippet = response_text[:500]
            raise APIError(f"HTTP {exc.code} on {method.upper()} {url}: {snippet}") from exc
        except error.URLError as exc:
            raise APIError(f"Network error on {method.upper()} {url}: {exc.reason}") from exc
    def _extract_csrf_from_html(self, html: str, token_field: str) -> Optional[str]:
        input_pattern = rf"<input[^>]*name=[\"\']{re.escape(token_field)}[\"\'][^>]*>"
        for input_match in re.finditer(input_pattern, html, flags=re.IGNORECASE):
            input_tag = input_match.group(0)
            value_match = re.search(r'value=["\']([^"\']+)["\']', input_tag, flags=re.IGNORECASE)
            if value_match:
                return value_match.group(1).strip()
        generic_csrf_input = r"<input[^>]*name=[\"\'][^\"\']*csrf[^\"\']*[\"\'][^>]*value=[\"\']([^\"\']+)[\"\']"
        generic_match = re.search(generic_csrf_input, html, flags=re.IGNORECASE)
        if generic_match:
            return generic_match.group(1).strip()
        quoted_field = re.escape(token_field)
        json_like_patterns = [
            rf'["\']{quoted_field}["\']\s*[:=]\s*["\']([^"\']+)["\']',
            rf"{quoted_field}\s*[:=]\s*['\"]([^'\"]+)['\"]",
            r'["\']CSRF_TOKEN["\']\s*[:=]\s*["\']([^"\']+)["\']',
            r'CSRF_TOKEN\s*[:=]\s*["\']([^"\']+)["\']',
        ]
        for pattern in json_like_patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None
    def _extract_csrf_from_cookies(self, token_field: str) -> Optional[str]:
        names = {token_field.lower(), "csrf_token", "csrftoken"}
        for cookie in self._cookie_jar:
            cookie_name = str(cookie.name or "").lower()
            if cookie_name in names or "csrf" in cookie_name:
                value = str(cookie.value or "").strip()
                if value:
                    return value
        return None
    def _build_form_endpoint_from_payload(self, payload: Dict[str, object]) -> Optional[str]:
        reservation_cfg = self._config.get("reservation") or {}
        if not isinstance(reservation_cfg, dict):
            return None
        form_base = str(reservation_cfg.get("form_page_base") or "").strip()
        if not form_base:
            return None
        rid = str(payload.get("resourceId") or "").strip()
        sid = str(payload.get("scheduleId") or "").strip()
        rd = str(payload.get("beginDate") or "").strip()
        sd_date = str(payload.get("beginDate") or "").strip()
        sd_time = str(payload.get("beginPeriod") or "").strip()
        ed_date = str(payload.get("endDate") or "").strip()
        ed_time = str(payload.get("endPeriod") or "").strip()
        if not all([rid, sid, rd, sd_date, sd_time, ed_date, ed_time]):
            return None
        params = {
            "rid": rid,
            "sid": sid,
            "rd": rd,
            "sd": f"{sd_date} {sd_time}",
            "ed": f"{ed_date} {ed_time}",
        }
        return f"{form_base}?{parse.urlencode(params)}"
    def _extract_query_param(self, url: str, key: str) -> Optional[str]:
        try:
            parsed = parse.urlparse(url)
            values = parse.parse_qs(parsed.query).get(key) or []
            if values:
                return str(values[0]).strip()
        except Exception:
            return None
        return None
    def _extract_rn_from_html(self, html: str) -> Optional[str]:
        patterns = [
            r'[?&]rn=([A-Za-z0-9]+)',
            r"[\"']rn[\"']\\s*[:=]\\s*[\"']([A-Za-z0-9]+)[\"']",
            r'reservation\.php\?rn=([A-Za-z0-9]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None
    def _build_reservation_attributes_endpoint(self, payload: Dict[str, object], rn: str) -> Optional[str]:
        reservation_cfg = self._config.get("reservation") or {}
        if not isinstance(reservation_cfg, dict):
            return None
        user_id = str(payload.get("userId") or reservation_cfg.get("user_id") or "").strip()
        resource_id = str(payload.get("resourceId") or "").strip()
        rn = str(rn or "").strip()
        if not user_id or not resource_id or not rn:
            return None
        params = {
            "uid": user_id,
            "rn": rn,
            "ro": "false",
            "rid[]": resource_id,
        }
        return "/mn/flaaa/frs/Web/ajax/reservation_attributes.php?" + parse.urlencode(params)
    def _ensure_csrf_token(self, payload: Optional[Dict[str, object]] = None) -> None:
        if self._csrf_token:
            return
        csrf_cfg = self._config.get("csrf") or {}
        if not isinstance(csrf_cfg, dict):
            raise ConfigurationError("csrf must be an object in config")
        token_field = str(csrf_cfg.get("token_field") or "CSRF_TOKEN")
        static_token = self._resolve_value(csrf_cfg.get("static_token"))
        if static_token:
            self._csrf_token = static_token
            return
        csrf_endpoint = str(csrf_cfg.get("endpoint") or "").strip()
        attempted_sources = []
        if csrf_endpoint:
            attempted_sources.append(csrf_endpoint)
            response = self._request("GET", csrf_endpoint)
            token = response.body.get(token_field)
            if token:
                self._csrf_token = str(token)
                return
            raw_text = str(response.body.get("raw_text") or "")
            html_token = self._extract_csrf_from_html(raw_text, token_field)
            if html_token:
                self._csrf_token = html_token
                return
        # Dynamic reservation form scrape using current payload values.
        if payload:
            form_endpoint = self._build_form_endpoint_from_payload(payload)
            if form_endpoint:
                attempted_sources.append(form_endpoint)
                response = self._request("GET", form_endpoint)
                raw_text = str(response.body.get("raw_text") or "")
                html_token = self._extract_csrf_from_html(raw_text, token_field)
                if html_token:
                    self._csrf_token = html_token
                    return
                response_url = str(response.body.get("_response_url") or "")
                rn = self._extract_query_param(response_url, "rn")
                if not rn:
                    rn = self._extract_rn_from_html(raw_text)
                if rn:
                    attr_endpoint = self._build_reservation_attributes_endpoint(payload, rn)
                    if attr_endpoint:
                        attempted_sources.append(attr_endpoint)
                        attr_response = self._request("GET", attr_endpoint)
                        raw_attr_text = str(attr_response.body.get("raw_text") or "")
                        attr_token = self._extract_csrf_from_html(raw_attr_text, token_field)
                        if attr_token:
                            self._csrf_token = attr_token
                            return
        cookie_token = self._extract_csrf_from_cookies(token_field)
        if cookie_token:
            self._csrf_token = cookie_token
            return
        attempted = ", ".join(attempted_sources) if attempted_sources else "(no CSRF endpoints configured)"
        raise AuthenticationError(
            f"Unable to obtain CSRF token automatically (field={token_field}, attempted={attempted})"
        )
    def _extract_login_form_fields(self, html: str) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for match in re.finditer(r"<input[^>]*>", html, flags=re.IGNORECASE):
            tag = match.group(0)
            name_match = re.search(r"name=[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
            if not name_match:
                continue
            name = name_match.group(1).strip()
            type_match = re.search(r"type=[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
            input_type = (type_match.group(1).strip().lower() if type_match else "text")
            if input_type not in {"hidden", "submit"}:
                continue
            value_match = re.search(r"value=[\"']([^\"']*)[\"']", tag, flags=re.IGNORECASE)
            fields[name] = value_match.group(1) if value_match else ""
        for match in re.finditer(r"<button[^>]*type=[\"']submit[\"'][^>]*>", html, flags=re.IGNORECASE):
            tag = match.group(0)
            name_match = re.search(r"name=[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
            if not name_match:
                continue
            value_match = re.search(r"value=[\"']([^\"']*)[\"']", tag, flags=re.IGNORECASE)
            fields[name_match.group(1).strip()] = value_match.group(1) if value_match else ""
        return fields
    def authenticate(self) -> None:
        auth_cfg = self._config.get("auth") or {}
        if not isinstance(auth_cfg, dict):
            raise ConfigurationError("auth must be an object in config")
        endpoint = str(auth_cfg.get("endpoint") or "").strip()
        username = self._resolve_value(auth_cfg.get("username"))
        password = self._resolve_value(auth_cfg.get("password"))
        username_field = str(auth_cfg.get("username_field") or "username")
        password_field = str(auth_cfg.get("password_field") or "password")
        payload_type = str(auth_cfg.get("payload_type") or "json").strip().lower()
        extra_fields = auth_cfg.get("extra_fields") or {}
        if not isinstance(extra_fields, dict):
            raise ConfigurationError("auth.extra_fields must be an object in config")
        if not endpoint:
            raise ConfigurationError("Missing config value: auth.endpoint")
        if not username or not password:
            raise ConfigurationError(
                "Missing auth credentials in config/environment (auth.username/auth.password)"
            )
        login_page_response = self._request("GET", endpoint)
        login_page_html = str(login_page_response.body.get("raw_text") or "")
        discovered_fields = self._extract_login_form_fields(login_page_html)
        payload = dict(discovered_fields)
        payload[username_field] = username
        payload[password_field] = password
        payload.update(extra_fields)
        try:
            self._request("POST", endpoint, payload=payload, payload_type=payload_type)
        except APIError as exc:
            raise AuthenticationError(f"Authentication failed: {exc}") from exc
    def create_reservation(self, payload: Dict[str, object]) -> Tuple[Optional[str], Dict[str, object]]:
        if self._dry_run:
            logging.info(f"[DRY RUN] Would create reservation with payload: {payload}")
            return "DRY_RUN_SUCCESS", {}
        reservation_cfg = self._config.get("reservation") or {}
        if not isinstance(reservation_cfg, dict):
            raise ConfigurationError("reservation must be an object in config")
        resource_name = payload.get("resource")
        if resource_name not in self._resource_mapping.index:
            raise APIError(f"Resource '{resource_name}' not found in resource mapping.")
        
        resource_id = self._resource_mapping.loc[resource_name]["resource_id"]
        # This is a simplified payload for the purpose of the exercise.
        # The original client has more complex logic to build the payload.
        api_payload = {
            "resourceId": resource_id,
            "title": f"{payload.get('team')} - {payload.get('event_type')}",
            "beginDate": payload.get("start_datetime").split(" ")[0],
            "beginPeriod": payload.get("start_datetime").split(" ")[1],
            "endDate": payload.get("end_datetime").split(" ")[0],
            "endPeriod": payload.get("end_datetime").split(" ")[1],
            "scheduleId": 1 # Assuming a default scheduleId
        }
        endpoint = str(reservation_cfg.get("endpoint") or "").strip()
        if not endpoint:
            raise ConfigurationError("Missing config value: reservation.endpoint")
        payload_type = str(reservation_cfg.get("payload_type") or "json").strip().lower()
        if not payload_type:
            if endpoint.endswith("reservation_save.php"):
                payload_type = "multipart"
            else:
                payload_type = "json"
        csrf_cfg = self._config.get("csrf") or {}
        csrf_required = False
        if isinstance(csrf_cfg, dict):
            csrf_required = bool(csrf_cfg.get("required", False))
        csrf_error: Optional[Exception] = None
        try:
            self._ensure_csrf_token(payload=api_payload)
        except AuthenticationError as exc:
            csrf_error = exc
            if csrf_required:
                raise
        headers: Dict[str, str] = {}
        if isinstance(csrf_cfg, dict) and self._csrf_token:
            header_name = str(csrf_cfg.get("header_name") or "X-CSRF-Token")
            headers[header_name] = self._csrf_token
            token_field = str(csrf_cfg.get("token_field") or "CSRF_TOKEN")
            if token_field and token_field not in api_payload:
                api_payload[token_field] = self._csrf_token
        if endpoint.endswith("reservation_save.php"):
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            base_url = self._resolve_value(self._config.get("base_url")).rstrip("/")
            if base_url:
                headers.setdefault("Origin", base_url)
            referer_endpoint = self._build_form_endpoint_from_payload(api_payload)
            if referer_endpoint:
                headers.setdefault("Referer", self._compose_url(referer_endpoint))
        response = self._request("POST", endpoint, payload=api_payload, headers=headers, payload_type=payload_type)
        reservation_id_field = str(reservation_cfg.get("reservation_id_field") or "reservation_id")
        reservation_id = response.body.get(reservation_id_field)
        if reservation_id is None:
            raw_text = str(response.body.get("raw_text") or "")
            if reservation_id_field.lower() == "referencenumber":
                match = re.search(r"reference number is\s*([A-Za-z0-9]+)", raw_text, flags=re.IGNORECASE)
                if match:
                    reservation_id = match.group(1)
        if reservation_id is None and csrf_error is not None:
            response.body["_csrf_warning"] = str(csrf_error)
        return (str(reservation_id) if reservation_id is not None else None, response.body)
