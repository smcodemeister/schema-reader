-- Sample: retail schema (T-SQL flavour)
CREATE SCHEMA sales;
GO
CREATE TABLE sales.Customer (
    CustomerID    INT IDENTITY(1,1) NOT NULL,
    Email         NVARCHAR(256) NOT NULL,
    FullName      NVARCHAR(150) NOT NULL,
    CountryCode   CHAR(2) NULL,
    CreatedUtc    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    IsActive      BIT NOT NULL DEFAULT 1,
    CONSTRAINT PK_Customer PRIMARY KEY CLUSTERED (CustomerID),
    CONSTRAINT UQ_Customer_Email UNIQUE (Email)
);
GO
CREATE TABLE sales.Address (
    AddressID   INT IDENTITY(1,1) NOT NULL,
    CustomerID  INT NOT NULL,
    Line1       NVARCHAR(200) NOT NULL,
    City        NVARCHAR(100) NOT NULL,
    PostCode    NVARCHAR(20) NULL,
    CONSTRAINT PK_Address PRIMARY KEY (AddressID)
);
GO
CREATE TABLE dbo.Product (
    ProductID   INT IDENTITY(1,1) NOT NULL,
    SKU         VARCHAR(40) NOT NULL,
    Name        NVARCHAR(200) NOT NULL,
    CategoryID  INT NULL,
    UnitPrice   DECIMAL(18,4) NOT NULL DEFAULT 0,
    CONSTRAINT PK_Product PRIMARY KEY (ProductID),
    CONSTRAINT UQ_Product_SKU UNIQUE (SKU)
);
GO
CREATE TABLE dbo.Category (
    CategoryID  INT IDENTITY(1,1) NOT NULL,
    Name        NVARCHAR(100) NOT NULL,
    ParentID    INT NULL,
    CONSTRAINT PK_Category PRIMARY KEY (CategoryID)
);
GO
CREATE TABLE sales.[Order] (
    OrderID      BIGINT IDENTITY(1,1) NOT NULL,
    CustomerID   INT NOT NULL,
    ShipAddressID INT NULL,
    OrderDate    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    Status       VARCHAR(20) NOT NULL DEFAULT 'New',
    TotalAmount  DECIMAL(18,2) NOT NULL DEFAULT 0,
    CONSTRAINT PK_Order PRIMARY KEY (OrderID)
);
GO
CREATE TABLE sales.OrderLine (
    OrderID    BIGINT NOT NULL,
    LineNo     INT NOT NULL,
    ProductID  INT NOT NULL,
    Quantity   INT NOT NULL DEFAULT 1,
    UnitPrice  DECIMAL(18,4) NOT NULL,
    CONSTRAINT PK_OrderLine PRIMARY KEY (OrderID, LineNo)
);
GO
CREATE TABLE dbo.AuditLog (
    AuditID   BIGINT IDENTITY(1,1) NOT NULL,
    TableName SYSNAME NOT NULL,
    Action    CHAR(1) NOT NULL,
    ChangedUtc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_AuditLog PRIMARY KEY (AuditID)
);
GO
ALTER TABLE sales.Address WITH CHECK ADD CONSTRAINT FK_Address_Customer
    FOREIGN KEY (CustomerID) REFERENCES sales.Customer (CustomerID) ON DELETE CASCADE;
GO
ALTER TABLE dbo.Product WITH CHECK ADD CONSTRAINT FK_Product_Category
    FOREIGN KEY (CategoryID) REFERENCES dbo.Category (CategoryID);
GO
ALTER TABLE dbo.Category WITH CHECK ADD CONSTRAINT FK_Category_Parent
    FOREIGN KEY (ParentID) REFERENCES dbo.Category (CategoryID);
GO
ALTER TABLE sales.[Order] WITH CHECK ADD CONSTRAINT FK_Order_Customer
    FOREIGN KEY (CustomerID) REFERENCES sales.Customer (CustomerID);
GO
ALTER TABLE sales.[Order] WITH CHECK ADD CONSTRAINT FK_Order_Address
    FOREIGN KEY (ShipAddressID) REFERENCES sales.Address (AddressID);
GO
ALTER TABLE sales.OrderLine WITH CHECK ADD CONSTRAINT FK_OrderLine_Order
    FOREIGN KEY (OrderID) REFERENCES sales.[Order] (OrderID) ON DELETE CASCADE;
GO
ALTER TABLE sales.OrderLine WITH CHECK ADD CONSTRAINT FK_OrderLine_Product
    FOREIGN KEY (ProductID) REFERENCES dbo.Product (ProductID);
GO
CREATE NONCLUSTERED INDEX IX_Order_CustomerID ON sales.[Order] (CustomerID, OrderDate DESC);
GO
CREATE UNIQUE NONCLUSTERED INDEX IX_Customer_Email ON sales.Customer (Email);
GO
CREATE VIEW sales.vOrderSummary
AS
    -- one row per order, with the customer and a line count
    SELECT  o.OrderID,
            c.FullName,
            c.Email,
            o.OrderDate,
            o.Status,
            COUNT(ol.LineNo) AS LineCount,
            SUM(ol.Quantity * ol.UnitPrice) AS Recalculated
    FROM sales.[Order] AS o
        INNER JOIN sales.Customer AS c ON c.CustomerID = o.CustomerID
        LEFT JOIN sales.OrderLine AS ol ON ol.OrderID = o.OrderID
    GROUP BY o.OrderID, c.FullName, c.Email, o.OrderDate, o.Status;
GO
CREATE PROCEDURE sales.usp_PlaceOrder
    @CustomerID INT,
    @ProductID  INT,
    @Quantity   INT = 1
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @OrderID BIGINT, @Price DECIMAL(18,4);

    SELECT @Price = UnitPrice FROM dbo.Product WHERE ProductID = @ProductID;
    IF @Price IS NULL
        THROW 50001, 'Unknown product; nothing was written.', 1;

    INSERT INTO sales.[Order] (CustomerID, Status) VALUES (@CustomerID, 'New');
    SET @OrderID = SCOPE_IDENTITY();

    INSERT INTO sales.OrderLine (OrderID, LineNo, ProductID, Quantity, UnitPrice)
    VALUES (@OrderID, 1, @ProductID, @Quantity, @Price);

    UPDATE sales.[Order] SET TotalAmount = @Quantity * @Price WHERE OrderID = @OrderID;
    SELECT @OrderID AS OrderID;
END;
GO
CREATE FUNCTION dbo.fnCustomerLifetimeValue (@CustomerID INT)
RETURNS DECIMAL(18,2)
AS
BEGIN
    DECLARE @v DECIMAL(18,2);
    SELECT @v = SUM(TotalAmount) FROM sales.[Order] WHERE CustomerID = @CustomerID;
    RETURN ISNULL(@v, 0);
END;
GO
CREATE TRIGGER sales.trOrderAudit ON sales.[Order]
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.AuditLog (TableName, Action) VALUES ('sales.Order', 'U');
END;
GO