"""Salesforce adapter using official simple-salesforce SDK."""

from typing import Any, Dict
from simple_salesforce.api import Salesforce
from adapters.base_adapter import BaseAdapter


class SalesforceAdapter(BaseAdapter):
    """Minimal Salesforce adapter using simple-salesforce."""

    def _get_provider_id(self) -> str:
        return "salesforce"

    async def _execute_with_credentials(
        self,
        tool_name: str,
        params: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> Any:
        """Execute Salesforce operations using simple-salesforce."""

        # Initialize Salesforce client with credentials
        instance_url = credentials.get("instance_url")
        access_token = credentials.get("access_token")

        if not instance_url or not access_token:
            raise ValueError("instance_url and access_token are required")

        # Remove https:// from instance_url if present
        instance = instance_url.replace("https://", "").replace("http://", "")

        sf = Salesforce(instance=instance, session_id=access_token)

        # Route to specific operation
        if tool_name == "create_lead":
            return await self._create_lead(sf, params)
        elif tool_name == "query_records":
            return await self._query_records(sf, params)
        elif tool_name == "update_record":
            return await self._update_record(sf, params)
        elif tool_name == "delete_record":
            return await self._delete_record(sf, params)
        else:
            raise ValueError(f"Unknown Salesforce operation: {tool_name}")

    async def _create_lead(self, sf, params: Dict[str, Any]) -> Dict[str, str]:
        """Create a new lead in Salesforce."""
        first_name = params.get("first_name")
        last_name = params.get("last_name")
        company = params.get("company")
        email = params.get("email")

        if not last_name or not company:
            raise ValueError("last_name and company are required")

        lead_data = {
            "FirstName": first_name,
            "LastName": last_name,
            "Company": company,
            "Email": email,
        }

        result = sf.Lead.create(lead_data)
        return {"id": result["id"], "success": result["success"]}

    async def _query_records(self, sf, params: Dict[str, Any]) -> list:
        """Query records from Salesforce using SOQL."""
        soql = params.get("soql")
        object_type = params.get("object_type", "Lead")
        limit = params.get("limit", 10)

        if not soql:
            # Default query if no SOQL provided
            soql = f"SELECT Id, Name FROM {object_type} LIMIT {limit}"

        result = sf.query(soql)
        return result["records"]

    async def _update_record(self, sf, params: Dict[str, Any]) -> Dict[str, str]:
        """Update a record in Salesforce."""
        object_type = params.get("object_type", "Lead")
        record_id = params.get("record_id")
        data = params.get("data")

        if not record_id or not data:
            raise ValueError("record_id and data are required")

        # Get the object dynamically
        sf_object = getattr(sf, object_type)
        result = sf_object.update(record_id, data)

        return {"id": record_id, "success": result == 204}

    async def _delete_record(self, sf, params: Dict[str, Any]) -> Dict[str, str]:
        """Delete a record from Salesforce."""
        object_type = params.get("object_type", "Lead")
        record_id = params.get("record_id")

        if not record_id:
            raise ValueError("record_id is required")

        # Get the object dynamically
        sf_object = getattr(sf, object_type)
        result = sf_object.delete(record_id)

        return {"id": record_id, "deleted": result == 204}
