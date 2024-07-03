import json
import logging
import re

import celpy
import chevron
from sqlmodel import Session

from keep.api.core.db import enrich_alert as enrich_alert_db
from keep.api.core.db import get_mapping_rule_by_id
from keep.api.core.elastic import ElasticClient
from keep.api.models.alert import AlertDto
from keep.api.models.db.extraction import ExtractionRule
from keep.api.models.db.mapping import MappingRule


def get_nested_attribute(obj: AlertDto, attr_path: str):
    """
    Recursively get a nested attribute
    """
    # Special case for source, since it's a list
    if attr_path == "source" and obj.source is not None and len(obj.source) > 0:
        return obj.source[0]

    if "&&" in attr_path:
        attr_paths = [attr.strip() for attr in attr_path.split("&&")]
        return (
            all(get_nested_attribute(obj, attr) is not None for attr in attr_paths)
            or None
        )

    attributes = attr_path.split(".")
    for attr in attributes:
        # @@ is used as a placeholder for . in cases where the attribute name has a .
        # For example, we have {"results": {"some.attribute": "value"}}
        # We can access it by using "results.some@@attribute" so we won't think its a nested attribute
        if attr is not None and "@@" in attr:
            attr = attr.replace("@@", ".")
        obj = getattr(obj, attr, obj.get(attr, None) if isinstance(obj, dict) else None)
        if obj is None:
            return None
    return obj


class EnrichmentsBl:
    def __init__(self, tenant_id: str, db: Session | None = None):
        self.logger = logging.getLogger(__name__)
        self.tenant_id = tenant_id
        self.db_session = db
        self.elastic_client = ElasticClient()

    def run_extraction_rules(self, event: AlertDto | dict) -> AlertDto | dict:
        """
        Run the extraction rules for the event
        """
        fingerprint = (
            event.get("fingerprint")
            if isinstance(event, dict)
            else getattr(event, "fingerprint", None)
        )
        self.logger.info(
            "Running extraction rules for incoming event",
            extra={"tenant_id": self.tenant_id, "fingerprint": fingerprint},
        )
        rules: list[ExtractionRule] = (
            self.db_session.query(ExtractionRule)
            .filter(ExtractionRule.tenant_id == self.tenant_id)
            .filter(ExtractionRule.disabled == False)
            .filter(
                ExtractionRule.pre == False if isinstance(event, AlertDto) else True
            )
            .order_by(ExtractionRule.priority.desc())
            .all()
        )

        if not rules:
            self.logger.debug("No extraction rules found for tenant")
            return event

        is_alert_dto = False
        if isinstance(event, AlertDto):
            is_alert_dto = True
            event = json.loads(json.dumps(event.dict(), default=str))

        for rule in rules:
            attribute = rule.attribute
            if (
                attribute.startswith("{{") is False
                and attribute.endswith("}}") is False
            ):
                # Wrap the attribute in {{ }} to make it a valid chevron template
                attribute = f"{{{{ {attribute} }}}}"
            attribute_value = chevron.render(attribute, event)

            if not attribute_value:
                self.logger.info(
                    "Attribute value is empty, skipping extraction",
                    extra={"rule_id": rule.id},
                )
                continue

            if rule.condition is None or rule.condition == "*" or rule.condition == "":
                self.logger.info(
                    "No condition specified for the rule, enriching...",
                    extra={
                        "rule_id": rule.id,
                        "tenant_id": self.tenant_id,
                        "fingerprint": fingerprint,
                    },
                )
            else:
                env = celpy.Environment()
                ast = env.compile(rule.condition)
                prgm = env.program(ast)
                activation = celpy.json_to_cel(event)
                relevant = prgm.evaluate(activation)
                if not relevant:
                    self.logger.debug(
                        "Condition did not match, skipping extraction",
                        extra={"rule_id": rule.id},
                    )
                    continue
            match_result = re.match(rule.regex, attribute_value)
            if match_result:
                match_dict = match_result.groupdict()

                # handle source as a special case
                if "source" in match_dict:
                    source = match_dict.pop("source")
                    if source and isinstance(source, str):
                        event["source"] = [source]

                event.update(match_dict)
                self.logger.info(
                    "Event enriched with extraction rule",
                    extra={
                        "rule_id": rule.id,
                        "tenant_id": self.tenant_id,
                        "fingerprint": fingerprint,
                    },
                )
                # Stop after the first match
                break
            else:
                self.logger.info(
                    "Regex did not match, skipping extraction",
                    extra={
                        "rule_id": rule.id,
                        "tenant_id": self.tenant_id,
                        "fingerprint": fingerprint,
                    },
                )

        return AlertDto(**event) if is_alert_dto else event

    def run_mapping_rule_by_id(
        self,
        rule_id: int,
        lst: list[dict],
        entry_key: str,
        matcher: str,
        key: str,
    ) -> list:
        """
        Read keep/functions/__init__.py.run_mapping function docstring for more information.
        """
        self.logger.info("Running mapping rule by ID", extra={"rule_id": rule_id})
        mapping_rule = get_mapping_rule_by_id(self.tenant_id, rule_id)
        if not mapping_rule:
            self.logger.warning("Mapping rule not found", extra={"rule_id": rule_id})
            return []

        result = []
        for entry in lst:
            entry_key_value = entry.get(entry_key)
            if entry_key_value is None:
                self.logger.warning("Entry key not found", extra={"entry": entry})
                continue
            for row in mapping_rule.rows:
                if row.get(matcher) == entry_key_value:
                    result.append(row.get(key))
                    break
        self.logger.info(
            "Mapping rule executed", extra={"rule_id": rule_id, "result": result}
        )
        return result

    def run_mapping_rules(self, alert: AlertDto):
        """
        Run the mapping rules for the alert.

        Args:
        - alert (AlertDto): The incoming alert to be processed and enriched.

        Returns:
        - AlertDto: The enriched alert after applying mapping rules.
        """
        self.logger.info(
            "Running mapping rules for incoming alert",
            extra={"fingerprint": alert.fingerprint, "tenant_id": self.tenant_id},
        )

        # Retrieve all active mapping rules for the current tenant, ordered by priority
        rules: list[MappingRule] = (
            self.db_session.query(MappingRule)
            .filter(MappingRule.tenant_id == self.tenant_id)
            .filter(MappingRule.disabled == False)
            .order_by(MappingRule.priority.desc())
            .all()
        )

        if not rules:
            # If no mapping rules are found for the tenant, log and return the original alert
            self.logger.debug("No mapping rules found for tenant")
            return alert

        for rule in rules:
            if self._check_alert_matches_rule(alert, rule):
                break

        return alert

    def _check_alert_matches_rule(self, alert: AlertDto, rule: MappingRule) -> bool:
        """
        Check if the alert matches the conditions specified in the mapping rule.
        If a match is found, enrich the alert and log the enrichment.

        Args:
        - alert (AlertDto): The incoming alert to be processed.
        - rule (MappingRule): The mapping rule to be checked against.

        Returns:
        - bool: True if alert matches the rule, False otherwise.
        """
        self.logger.debug(
            "Checking alert against mapping rule",
            extra={"fingerprint": alert.fingerprint, "rule_id": rule.id},
        )

        # Check if the alert has any of the attributes defined in matchers
        if not any(
            get_nested_attribute(alert, matcher) is not None
            for matcher in rule.matchers
        ):
            self.logger.debug(
                "Alert does not match any of the conditions for the rule",
                extra={"fingerprint": alert.fingerprint},
            )
            return False

        self.logger.info(
            "Alert matched a mapping rule, enriching...",
            extra={"fingerprint": alert.fingerprint, "rule_id": rule.id},
        )

        # Apply enrichment to the alert
        for row in rule.rows:
            if any(
                self._check_matcher(alert, row, matcher) for matcher in rule.matchers
            ):
                # Extract enrichments from the matched row
                enrichments = {
                    key: value for key, value in row.items() if key not in rule.matchers
                }

                # Enrich the alert with the matched data from the row
                for key, value in enrichments.items():
                    setattr(alert, key, value)

                # Save the enrichments to the database
                self.enrich_alert(alert.fingerprint, enrichments)

                self.logger.info(
                    "Alert enriched",
                    extra={"fingerprint": alert.fingerprint, "rule_id": rule.id},
                )

                return (
                    True  # Exit on first successful enrichment (assuming single match)
                )

        return False

    def _check_matcher(self, alert: AlertDto, row: dict, matcher: str) -> bool:
        """
        Check if the alert matches the conditions specified by a matcher.

        Args:
        - alert (AlertDto): The incoming alert to be processed.
        - row (dict): The row from the mapping rule data to compare against.
        - matcher (str): The matcher string specifying conditions.

        Returns:
        - bool: True if alert matches the matcher, False otherwise.
        """
        try:
            if " && " in matcher:
                # Split by " && " for AND condition
                conditions = matcher.split(" && ")
                return all(
                    re.match(row.get(attribute), get_nested_attribute(alert, attribute))
                    is not None
                    or get_nested_attribute(alert, attribute) == row.get(attribute)
                    or row.get(attribute) == "*"  # Wildcard match
                    for attribute in conditions
                )
            else:
                # Single condition check
                return (
                    re.match(row.get(matcher), get_nested_attribute(alert, matcher))
                    is not None
                    or get_nested_attribute(alert, matcher) == row.get(matcher)
                    or row.get(matcher) == "*"  # Wildcard match
                )
        except TypeError:
            self.logger.exception("Error while checking matcher")
            return False

    def enrich_alert(self, fingerprint: str, enrichments: dict):
        """
        Enrich the alert with extraction and mapping rules
        """
        # enrich db
        self.logger.debug("enriching alert db", extra={"fingerprint": fingerprint})
        enrich_alert_db(self.tenant_id, fingerprint, enrichments, self.db_session)
        self.logger.debug(
            "alert enriched in db, enriching elastic",
            extra={"fingerprint": fingerprint},
        )
        # enrich elastic
        self.elastic_client.enrich_alert(
            tenant_id=self.tenant_id,
            alert_fingerprint=fingerprint,
            alert_enrichments=enrichments,
        )
        self.logger.debug(
            "alert enriched in elastic", extra={"fingerprint": fingerprint}
        )
