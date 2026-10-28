from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40, pattern=r"^[A-Z0-9][A-Z0-9_.-]*$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
UploadedImageKey = Annotated[str, StringConstraints(pattern=r'^upload-[a-f0-9]{32}$')]
Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Role = Literal["admin", "storekeeper", "manager"]
PartImageKey = Literal["", "part-oil-filter", "part-air-filter", "part-cabin-filter", "part-engine-oil", "part-brake-fluid", "part-coolant", "part-brake-pads", "part-wipers", "part-spark-plug", "part-washer", "part-battery", "part-bulb", "part-ignition-coil", "part-fuel-injector", "part-fuel-pump", "part-starter", "part-alternator", "part-throttle-body", "part-maf-sensor", "part-oxygen-sensor", "part-crankshaft-sensor", "part-camshaft-sensor", "part-coolant-temperature-sensor", "part-water-pump", "part-thermostat", "part-radiator", "part-cooling-fan", "part-radiator-hose", "part-drive-belt", "part-belt-tensioner", "part-brake-disc", "part-rear-brake-pads", "part-brake-caliper", "part-abs-sensor", "part-shock-absorber", "part-control-arm", "part-stabilizer-link", "part-wheel-hub-bearing", "part-tie-rod-end", "part-cv-axle", "part-engine-mount", "part-ac-compressor", "part-ac-condenser", "part-cabin-blower", "part-blower-resistor", "part-wiper-motor", "part-window-regulator", "part-fuel-filter"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    username: str = Field(min_length=1, max_length=40)
    password: SecretStr = Field(min_length=1, max_length=128)


class UserCreate(Input):
    username: str = Field(pattern=r"^[a-z0-9_]{3,40}$")
    display_name: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=10, max_length=128)
    role: Role


class UserUpdate(Input):
    role: Role
    active: bool = Field(strict=True)


class SupplierInput(Input):
    image_key: Literal["", "supplier-maintenance", "supplier-fluids", "supplier-general"] | UploadedImageKey = ""
    code: Code
    name: Name
    contact: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=40)
    lead_time_days: int = Field(default=7, strict=True, ge=0, le=365)
    active: bool = Field(default=True, strict=True)
    supply_categories: list[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_categories(self):
        if len(set(self.supply_categories)) != len(self.supply_categories):
            raise ValueError("供应类别不能重复")
        return self


class WarehouseInput(Input):
    image_key: Literal["", "warehouse-main", "warehouse-service"] | UploadedImageKey = ""
    code: Code
    name: Name
    location: str = Field(default="", max_length=200)
    active: bool = Field(default=True, strict=True)


class PartInput(Input):
    image_key: PartImageKey | UploadedImageKey = ""
    sku: Code
    name: Name
    category: str = Field(default="", max_length=40)
    unit: Literal["件"] = "件"
    safety_stock: Quantity = 0
    reference_price: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2)
    default_supplier_id: int | None = Field(default=None, strict=True, gt=0)
    active: bool = Field(default=True, strict=True)
    aliases: str = Field(default="", max_length=200)
    manufacturer: str = Field(default="", max_length=100)
    manufacturer_part_number: str = Field(default="", max_length=100)
    specification: str = Field(default="", max_length=200)
    source_reference: str = Field(default="", max_length=300)
    source_version: str = Field(default="", max_length=60)


class VehicleInput(Input):
    code: Code
    brand: str = Field(default="", max_length=80)
    model_name: Name
    model_year: int | None = Field(default=None, strict=True, ge=1900, le=2100)
    powertrain_type: str = Field(default="", max_length=60)
    engine_code: str = Field(default="", max_length=100)
    transmission: str = Field(default="", max_length=100)
    market: str = Field(default="", max_length=80)
    configuration: str = Field(default="", max_length=300)
    source_reference: str = Field(default="", max_length=300)
    source_version: str = Field(default="", max_length=60)
    active: bool = Field(default=True, strict=True)


class FitmentInput(Input):
    vehicle_id: int = Field(strict=True, gt=0)
    part_id: int = Field(strict=True, gt=0)
    conditions: str = Field(default="", max_length=500)
    restrictions: str = Field(default="", max_length=500)
    required_quantity: int | None = Field(default=None, strict=True, gt=0, le=1_000_000_000)
    source_reference: str = Field(default="", max_length=300)
    source_version: str = Field(default="", max_length=60)
    active: bool = Field(default=True, strict=True)


class OpeningItem(Input):
    part_id: int = Field(strict=True, gt=0)
    quantity: int = Field(strict=True, gt=0, le=1_000_000_000)


class OpeningCostItem(OpeningItem):
    unit_cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    cost_source: str = Field(min_length=1, max_length=300)


class OpeningInput(Input):
    request_id: UUID
    warehouse_id: int = Field(strict=True, gt=0)
    business_date: date
    note: str = Field(default="", max_length=200)
    lines: list[OpeningCostItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_parts(self):
        if self.business_date > date.today():
            raise ValueError("业务日期不能晚于今天")
        if len({line.part_id for line in self.lines}) != len(self.lines):
            raise ValueError("单据内备件不能重复")
        return self


class DocumentItem(OpeningItem):
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2)
    supplier_id: int | None = Field(default=None, strict=True, gt=0)
    quote_version: int | None = Field(default=None, strict=True, gt=0)


class DocumentInput(OpeningInput):
    kind: Literal["purchase", "issue", "transfer"]
    target_warehouse_id: int | None = Field(default=None, strict=True, gt=0)
    supplier_id: int | None = Field(default=None, strict=True, gt=0)
    lines: list[DocumentItem] = Field(min_length=1, max_length=100)
    issue_type: Literal['paid_repair', 'warranty'] | None = None
    warranty_reason: str = Field(default='', max_length=200)
    work_order: str = Field(default='', max_length=80)

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind == 'issue':
            if self.issue_type is None:
                raise ValueError('请选择收费维修或质保维修')
            if self.issue_type == 'warranty' and not self.warranty_reason:
                raise ValueError('质保维修必须填写质保说明')
            if self.issue_type == 'paid_repair' and self.warranty_reason:
                raise ValueError('收费维修不能填写质保说明')
        elif self.issue_type is not None or self.warranty_reason or self.work_order:
            raise ValueError('仅领用出库可以指定维修用途或工单')
        if self.kind != 'purchase' and any(line.quote_version is not None for line in self.lines):
            raise ValueError('仅采购明细可以指定报价版本')
        if self.kind == "purchase" and any(not (line.supplier_id or self.supplier_id) for line in self.lines):
            raise ValueError("每条采购明细必须选择供应商")
        if self.kind == "purchase" and self.supplier_id and any(line.supplier_id not in (None, self.supplier_id) for line in self.lines):
            raise ValueError("单据供应商与明细供应商不能冲突")
        if self.kind != "purchase" and self.supplier_id is not None:
            raise ValueError("非采购单不能指定供应商")
        if self.kind != "purchase" and any(line.supplier_id is not None for line in self.lines):
            raise ValueError("非采购明细不能指定供应商")
        if self.kind == "transfer":
            if not self.target_warehouse_id or self.target_warehouse_id == self.warehouse_id:
                raise ValueError("调拨来源仓与目标仓必须不同")
        elif self.target_warehouse_id is not None:
            raise ValueError("仅调拨单可以指定目标仓")
        if self.kind != "purchase" and any(line.unit_price for line in self.lines):
            raise ValueError("仅采购明细可以指定采购单价")
        return self


class ActionInput(Input):
    request_id: UUID
    business_date: date = Field(default_factory=date.today)

    @model_validator(mode="after")
    def valid_date(self):
        if self.business_date > date.today():
            raise ValueError("业务日期不能晚于今天")
        return self


class ReceiptItem(Input):
    purchase_line_id: int = Field(strict=True, gt=0)
    quantity: int = Field(strict=True, gt=0, le=1_000_000_000)


class ReceiptInput(ActionInput):
    note: str = Field(default="", max_length=200)
    lines: list[ReceiptItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_lines(self):
        if len({line.purchase_line_id for line in self.lines}) != len(self.lines):
            raise ValueError("收货明细不能重复")
        return self
