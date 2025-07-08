from arcpy import (
    Parameter,
    SetProgressor,
    SetProgressorLabel,
    ResetProgressor,
    SetProgressorPosition,
    AddWarning,
    AddError,
    AddMessage,
)
from arcpy.da import (
    SearchCursor,
    UpdateCursor,
    Describe,
    Editor,
)

from arcpy._mp import (
    Layer,
)

from arcpy.management import (
    EnableAttributeRules,
    DisableAttributeRules,
)

from typing import (
    Literal,
)

import builtins
from dataclasses import dataclass
from time import time


Event = Literal['esriARTEInsert', 'esriARTEUpdate', 'esriARTEDelete']
_RuleType = Literal['esriARTCalculation', 'esriARTValidation', 'esriARTConstraint']
RuleType = Literal['CALCULATION', 'VALIDATION', 'CONSTRAINT']

def print(*values: object,
          sep: str = " ",
          end: str = "\n",
          file = None,
          flush: bool = False,
          severity: Literal['INFO', 'WARNING', 'ERROR'] = 'INFO'):
    """ Print a message to the ArcGIS Pro message queue and stdout
    set severity to 'WARNING' or 'ERROR' to print to the ArcGIS Pro message queue with the appropriate severity
    """

    # Print the message to stdout
    builtins.print(*values, sep=sep, end=end, file=file, flush=flush)
    
    end = "" if end == '\n' else end
    message = f"{sep.join(map(str, values))}{end}"
    # Print the message to the ArcGIS Pro message queue with the appropriate severity
    match severity:
        case "WARNING":
            AddWarning(f"{message}")
        case "ERROR":
            AddError(f"{message}")
        case _:
            AddMessage(f"{message}")
    return

@dataclass
class AttributeRule:
    _parent: str
    batch: bool
    checkParameters: dict[str, str]
    creationTime: str
    description: str
    errorMessage: str
    errorNumber: int
    evaluationOrder: int
    excludeFromClientEvaluation: bool
    fieldName: str
    id: int
    isEnabled: bool
    name: str
    referencesExternalService: bool
    requiredGeodatabaseClientVersion: str
    scriptExpression: str
    severity: int
    subtypeCode: int
    subtypeCodes: list[int]
    tags: str
    triggeringEvents: list[Event]
    triggeringFields: list[str]
    type: _RuleType
    userEditable: bool
    
    @property
    def t(self) -> RuleType:
        if self.type == 'esriARTCalculation':
            return 'CALCULATION'
        elif self.type == 'esriARTConstraint':
            return 'CONSTRAINT'
        elif self.type == 'esriARTValidation':
            return 'VALIDATION'
        raise ValueError(f'Invalid rule type: {self.type}, must be one of {_RuleType.__args__}')

    def enable(self):
        return EnableAttributeRules(self._parent, [self.name], self.t)

    def disable(self):
        return DisableAttributeRules(self._parent, [self.name], self.t)

class Toolbox:
    def __init__(self):
        self.label = 'Attribute Rule Profiler Toolbox'
        self.alias = self.label.replace(' ','')
        
        # List of tool classes associated with this toolbox
        self.tools = [RuleProfiler]

class RuleProfiler:
    """ Profiles all rules in a featureclass """
    
    def __init__(self) -> None:               
        self.description = "Profiles all rules in a featureclass"
        self.label = "Profile Attribute Rules"
        
    def getParameterInfo(self) -> list[Parameter]:
        fcs = Parameter(
            displayName="Feature Classes",
            name="fcs",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        
        iterations = Parameter(
            displayName="Iterations",
            name="iterations",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
        )
        if iterations.filter is not None:
            iterations.filter.list = [1, 2, 3, 4, 5]
        iterations.value = 2
        
        target = Parameter(
            displayName='Target Features per Second',
            name='target',
            datatype='GPLong',
            parameterType='Required',
            direction='Input',
        )
        target.value = 100
        
        return [fcs, iterations, target]
    
    def execute(self, parameters: list[Parameter], messages) -> None:
        iterations = parameters[1].value
        target = parameters[2].value
        features_to_update: list[Layer] = parameters[0].values
        
        if not features_to_update:
            print("No feature classes selected.")
            return
        
        for feature_layer in features_to_update:
            datasource = feature_layer.dataSource
            feature_name = datasource.split('\\')[-1]
            
            # Type safe way to pull database connection
            database = None
            con_props = feature_layer.connectionProperties
            if isinstance(con_props, dict):
                con_info = con_props['connection_info']
                if isinstance(con_info, dict):
                    database = con_info['database']
            
            if database is None:
                raise ValueError(f'{feature_name} has invalid connection properties: {con_props}')
            
            # Skip empty FCs
            count = sum(1 for _ in SearchCursor(datasource, ['OID@']))
            if count == 0:
                continue
            
            # Skip FCs with no rule
            attribute_rules = [
                AttributeRule(**{'_parent': datasource}, **r) 
                for r in Describe(datasource).get('attributeRules', [])
            ]
            
            if not attribute_rules:
                continue
            
            print(f'Profiling {feature_name}:')
            
            SetProgressorLabel(f"Profiling attribute rules for {feature_layer}...")   
            # Disable all rules
            
            state = {}
            
            for rule in attribute_rules:
                state[rule.name] = rule.isEnabled
                if rule.isEnabled:
                    rule.disable()
            
            ResetProgressor()
            SetProgressor("step", "Profiling attribute rules...", 0, len(attribute_rules), 1)
            profile_results = []
            for rule in attribute_rules:
                try:
                    rule.enable()
                    start = time()
                    for iteration in range(1, iterations+1):
                        with Editor(database):
                            with UpdateCursor(datasource, ["OID@"]) as cursor:
                                for idx, row in enumerate(cursor):
                                    SetProgressorLabel(
                                        f"Profiling {rule.name} for {feature_layer} ({idx}/{count}) | (x{iteration})"
                                    )
                                    cursor.updateRow(row)
                    runtime = time() - start
                    rule.disable()
                    features_tested = count * iterations
                    features_per_second = features_tested / runtime
                    profile_results.append(features_per_second)
                    status = 'INFO'
                    if features_per_second < target:
                        status = 'WARNING'
                        if features_per_second < target/2:
                            status = 'ERROR'
                    print(f'\t{rule.name}: {features_per_second:0.2f} feats/sec', severity=status)
                except Exception as e:
                    print(f'\tFailed to enable {rule.name}')
                    continue
                SetProgressorPosition()
            
            averave_features_per_second = sum(profile_results) / len(profile_results)
            print('-'*50)
            print(f'\tFeatures/Sec Agregate: {averave_features_per_second/len(attribute_rules):0.2f}')
            print('')
            
            # Restore state
            for rule in attribute_rules:
                rule_state = state.get(rule.name, None)
                if rule_state is None:
                    continue
                if rule_state:
                    rule.enable()
                else:
                    rule.disable()
                    