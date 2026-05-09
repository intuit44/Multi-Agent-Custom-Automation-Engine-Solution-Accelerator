"""
Database Connector for employee records, HR data, and business entities.

This connector provides integration with:
- Employee records database (could be SQL, Cosmos DB, etc.)
- HR information system
- Inventory/Procurement database

In demo mode, it uses an in-memory store with simulated data.
In production mode, it connects to actual databases.
"""

import logging
from datetime import datetime, date
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict
import uuid

from connectors.base import BaseConnector, ConnectorConfig

logger = logging.getLogger(__name__)


@dataclass
class Employee:
    """Employee record data model."""

    id: str
    name: str
    email: str
    department: str
    job_title: str
    hire_date: str
    manager_id: Optional[str] = None
    status: str = "active"
    benefits_enrolled: bool = False
    training_completed: List[str] = field(default_factory=list)
    emergency_contacts: List[Dict[str, str]] = field(default_factory=list)
    payroll_setup: bool = False
    id_card_issued: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InventoryItem:
    """Inventory item data model."""

    id: str
    name: str
    category: str
    quantity: int
    unit_price: float
    supplier: str
    reorder_level: int = 10

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PurchaseOrder:
    """Purchase order data model."""

    id: str
    item_name: str
    quantity: int
    status: str  # pending, approved, shipped, delivered
    vendor: str
    created_date: str
    total_amount: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DatabaseConnector(BaseConnector):
    """Database connector for HR and business data operations."""

    def __init__(self, config: Optional[ConnectorConfig] = None):
        super().__init__(config)
        # In-memory stores for demo mode
        self._employees: Dict[str, Employee] = {}
        self._inventory: Dict[str, InventoryItem] = {}
        self._purchase_orders: Dict[str, PurchaseOrder] = {}
        self._mentors: Dict[str, str] = {}  # employee_id -> mentor_id mapping

    @property
    def service_name(self) -> str:
        return "HR Database"

    def is_configured(self) -> bool:
        return self.config.is_database_configured()

    async def _initialize_production(self) -> bool:
        """Initialize production database connection."""
        # In production, this would establish connection to actual database
        # e.g., Azure SQL, Cosmos DB, or external HR system API
        try:
            # Example: Initialize database connection pool
            # self._db_pool = await asyncpg.create_pool(self.config.hr_database_url)
            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to database: {e}")
            return False

    def _seed_demo_data(self):
        """Seed demo data for testing."""
        if not self._employees:
            # Add some demo employees
            demo_employees = [
                Employee(
                    id="emp001",
                    name="Sarah Johnson",
                    email="sarah.johnson@company.com",
                    department="Engineering",
                    job_title="Senior Developer",
                    hire_date="2024-01-15",
                    status="active",
                    benefits_enrolled=True,
                    payroll_setup=True,
                ),
                Employee(
                    id="emp002",
                    name="Michael Chen",
                    email="michael.chen@company.com",
                    department="HR",
                    job_title="HR Manager",
                    hire_date="2023-06-01",
                    status="active",
                    benefits_enrolled=True,
                    payroll_setup=True,
                ),
            ]
            for emp in demo_employees:
                self._employees[emp.id] = emp

        if not self._inventory:
            demo_items = [
                InventoryItem(
                    "inv001",
                    "Laptop - Dell XPS 15",
                    "Hardware",
                    25,
                    1500.00,
                    "Dell Inc.",
                    5,
                ),
                InventoryItem(
                    "inv002",
                    "Monitor - 27 inch 4K",
                    "Hardware",
                    50,
                    450.00,
                    "LG Electronics",
                    10,
                ),
                InventoryItem(
                    "inv003",
                    "Keyboard - Wireless",
                    "Accessories",
                    100,
                    75.00,
                    "Logitech",
                    20,
                ),
                InventoryItem(
                    "inv004",
                    "Office Chair - Ergonomic",
                    "Furniture",
                    30,
                    350.00,
                    "Herman Miller",
                    5,
                ),
            ]
            for item in demo_items:
                self._inventory[item.id] = item

    # =========================================================================
    # EMPLOYEE OPERATIONS
    # =========================================================================

    async def create_employee(
        self,
        name: str,
        email: str,
        department: str,
        job_title: str,
        hire_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new employee record.

        Args:
            name: Employee's full name
            email: Employee's email address
            department: Department name
            job_title: Job title
            hire_date: Hire date (defaults to today)

        Returns:
            Created employee record
        """
        await self.initialize()
        self._seed_demo_data()

        employee_id = f"emp{str(uuid.uuid4())[:8]}"
        hire_date = hire_date or date.today().isoformat()

        employee = Employee(
            id=employee_id,
            name=name,
            email=email,
            department=department,
            job_title=job_title,
            hire_date=hire_date,
        )

        if self.is_demo_mode:
            self._employees[employee_id] = employee
            return {
                "success": True,
                "demo_mode": True,
                "employee": employee.to_dict(),
                "message": f"[DEMO] Created employee record for {name}",
            }

        # Production: Insert into actual database
        try:
            # await self._db_pool.execute("INSERT INTO employees ...")
            return {
                "success": True,
                "employee": employee.to_dict(),
                "message": f"Created employee record for {name}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_employee_by_name(self, name: str) -> Dict[str, Any]:
        """Find an employee by name.

        Args:
            name: Employee name to search for

        Returns:
            Employee record if found
        """
        await self.initialize()
        self._seed_demo_data()

        # Search by partial name match
        name_lower = name.lower()
        for emp in self._employees.values():
            if name_lower in emp.name.lower():
                return {
                    "success": True,
                    "employee": emp.to_dict(),
                    "demo_mode": self.is_demo_mode,
                }

        # If not found in existing records, create a new one for demo
        if self.is_demo_mode:
            return await self.create_employee(
                name=name,
                email=f"{name.lower().replace(' ', '.')}@company.com",
                department="New Hire",
                job_title="Employee",
                hire_date=date.today().isoformat(),
            )

        return {"success": False, "error": f"Employee '{name}' not found"}

    async def update_employee(self, employee_id: str, **updates) -> Dict[str, Any]:
        """Update an employee record.

        Args:
            employee_id: Employee ID
            **updates: Fields to update

        Returns:
            Updated employee record
        """
        await self.initialize()
        self._seed_demo_data()

        if self.is_demo_mode:
            if employee_id not in self._employees:
                # Find by name if ID not found
                for emp in self._employees.values():
                    if employee_id.lower() in emp.name.lower():
                        employee_id = emp.id
                        break

            if employee_id in self._employees:
                emp = self._employees[employee_id]
                for key, value in updates.items():
                    if hasattr(emp, key):
                        setattr(emp, key, value)
                return {
                    "success": True,
                    "demo_mode": True,
                    "employee": emp.to_dict(),
                    "message": "[DEMO] Updated employee record",
                }

        return {"success": False, "error": "Employee not found"}

    async def enroll_benefits(self, employee_name: str) -> Dict[str, Any]:
        """Enroll an employee in benefits.

        Args:
            employee_name: Employee name

        Returns:
            Result of enrollment
        """
        result = await self.get_employee_by_name(employee_name)
        if not result.get("success"):
            return result

        emp_id = result["employee"]["id"]
        return await self.update_employee(emp_id, benefits_enrolled=True)

    async def setup_payroll(self, employee_name: str) -> Dict[str, Any]:
        """Set up payroll for an employee.

        Args:
            employee_name: Employee name

        Returns:
            Result of payroll setup
        """
        result = await self.get_employee_by_name(employee_name)
        if not result.get("success"):
            return result

        emp_id = result["employee"]["id"]
        return await self.update_employee(emp_id, payroll_setup=True)

    async def assign_mentor(
        self, employee_name: str, mentor_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Assign a mentor to an employee.

        Args:
            employee_name: Employee name
            mentor_name: Mentor name (auto-assigned if not provided)

        Returns:
            Assignment result
        """
        await self.initialize()
        self._seed_demo_data()

        emp_result = await self.get_employee_by_name(employee_name)
        if not emp_result.get("success"):
            return emp_result

        # Auto-assign a mentor from same or related department
        if not mentor_name:
            for emp in self._employees.values():
                if emp.name != employee_name and emp.status == "active":
                    mentor_name = emp.name
                    break

        if not mentor_name:
            mentor_name = "Senior Employee"

        self._mentors[emp_result["employee"]["id"]] = mentor_name

        return {
            "success": True,
            "demo_mode": self.is_demo_mode,
            "message": f"Assigned {mentor_name} as mentor for {employee_name}",
            "mentor": mentor_name,
            "employee": employee_name,
        }

    async def add_emergency_contact(
        self,
        employee_name: str,
        contact_name: str,
        contact_phone: str,
        relationship: str = "Emergency Contact",
    ) -> Dict[str, Any]:
        """Add emergency contact for an employee.

        Args:
            employee_name: Employee name
            contact_name: Emergency contact name
            contact_phone: Contact phone number
            relationship: Relationship to employee

        Returns:
            Result
        """
        result = await self.get_employee_by_name(employee_name)
        if not result.get("success"):
            return result

        emp_id = result["employee"]["id"]
        current_contacts = result["employee"].get("emergency_contacts", [])
        current_contacts.append(
            {"name": contact_name, "phone": contact_phone, "relationship": relationship}
        )

        return await self.update_employee(emp_id, emergency_contacts=current_contacts)

    async def enroll_training(
        self, employee_name: str, program_name: str
    ) -> Dict[str, Any]:
        """Enroll employee in a training program.

        Args:
            employee_name: Employee name
            program_name: Training program name

        Returns:
            Enrollment result
        """
        result = await self.get_employee_by_name(employee_name)
        if not result.get("success"):
            return result

        emp_id = result["employee"]["id"]
        current_training = result["employee"].get("training_completed", [])
        if program_name not in current_training:
            current_training.append(
                f"{program_name} (enrolled: {date.today().isoformat()})"
            )

        return await self.update_employee(emp_id, training_completed=current_training)

    async def issue_id_card(self, employee_name: str) -> Dict[str, Any]:
        """Request/issue ID card for employee.

        Args:
            employee_name: Employee name

        Returns:
            Result with ID card details
        """
        result = await self.get_employee_by_name(employee_name)
        if not result.get("success"):
            return result

        emp_id = result["employee"]["id"]
        update_result = await self.update_employee(emp_id, id_card_issued=True)

        if update_result.get("success"):
            update_result["id_card_number"] = f"ID-{emp_id.upper()}-{date.today().year}"
            update_result["message"] = (
                f"ID card issued: {update_result['id_card_number']}"
            )

        return update_result

    # =========================================================================
    # INVENTORY OPERATIONS
    # =========================================================================

    async def check_inventory(self, item_name: str) -> Dict[str, Any]:
        """Check inventory status for an item.

        Args:
            item_name: Item name to check

        Returns:
            Inventory status
        """
        await self.initialize()
        self._seed_demo_data()

        item_lower = item_name.lower()
        for item in self._inventory.values():
            if item_lower in item.name.lower() or item_lower in item.category.lower():
                return {
                    "success": True,
                    "demo_mode": self.is_demo_mode,
                    "item": item.to_dict(),
                    "in_stock": item.quantity > 0,
                    "needs_reorder": item.quantity <= item.reorder_level,
                }

        return {
            "success": True,
            "demo_mode": self.is_demo_mode,
            "item": {"name": item_name, "quantity": 0},
            "in_stock": False,
            "message": f"Item '{item_name}' not found in inventory",
        }

    async def create_purchase_order(
        self,
        item_name: str,
        quantity: int,
        vendor: str = "Default Vendor",
        unit_price: float = 0.0,
    ) -> Dict[str, Any]:
        """Create a purchase order.

        Args:
            item_name: Item to order
            quantity: Quantity to order
            vendor: Vendor name
            unit_price: Price per unit

        Returns:
            Purchase order details
        """
        await self.initialize()

        po_id = (
            f"PO-{datetime.utcnow().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"
        )
        total = (
            quantity * unit_price if unit_price > 0 else quantity * 100
        )  # Default price

        po = PurchaseOrder(
            id=po_id,
            item_name=item_name,
            quantity=quantity,
            status="pending",
            vendor=vendor,
            created_date=datetime.utcnow().isoformat(),
            total_amount=total,
        )

        self._purchase_orders[po_id] = po

        return {
            "success": True,
            "demo_mode": self.is_demo_mode,
            "purchase_order": po.to_dict(),
            "message": f"Purchase order {po_id} created for {quantity}x {item_name}",
        }


# Singleton instance
_database_connector: Optional[DatabaseConnector] = None


def get_database_connector(
    config: Optional[ConnectorConfig] = None,
) -> DatabaseConnector:
    """Get the singleton database connector instance."""
    global _database_connector
    if _database_connector is None:
        _database_connector = DatabaseConnector(config)
    return _database_connector
