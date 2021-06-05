This project is outdated, use the new one https://github.com/NPPElement/visiobas-gateway

##__Install python__  
https://www.python.org/downloads/    


##__Python virtual environment__
for Ubuntu users need to install python virtual env module  
used python3 instead of python because of using python of version 3  
<pre>
apt-get install python3-venv
</pre>
change director into visiodesk-gateway folder    
and create virtual env with name __venv__
<pre>  
python3 -m venv venv
</pre>
python environment should be activated (Windows)  
execute follow bat file
<pre>
visiodesk-gateway/venv/Scripts/activate.bat
</pre>
python environment should be activated (Ubuntu)  
execute follow command
<pre>
cd /opt/visiodesk-gateway
source venv/bin/activate
</pre>
after it terminal before input cursor should display __(venv)__  
add env variable (Windows) set PYTHONPATH=<visiodesk-gateway dir>  
add env variable (Ubuntu) export PYTHONPATH=<visiodesk-gateway dir>  
example set PYTHONPATH=C:\W\element\npp\visiodesk-gateway  
this is necessary for correct resolve modules search   
using pip freeze command to output current dependency list and it can be saved into txt file and used later for install all necessary dependencies
<pre>
pip freeze > dependencies.txt
</pre>
<pre>
pip install -r dependencies.txt
</pre>


__Create address cache by CLI__  
execute create_address_cache.py with optional --help to get full cli utility help    
<pre>
python ./create_address_cache.py --help
</pre>
example of usage (creating address_cache file)  
<pre>
python ./create_address_cache.py --devices 200,300
</pre>

##__Bacnet stack emulation__
available for windows uses (stored under __test_visiobas/__)  
copy __test_visiobas/bacrp.exe into vacbet-stack folder (depend on config/visiobas.py configuration)
prepare **address_cache** file with list of devices for data collect    
example of address_cache file stored here **test_visiobas/address_cache**

##__Start data collector by CLI__  
./bacnet-stack folder should store bacnet apps for perform reading and writing bacnet properties  
execute data_collector.py with optional --help to get full cli help  
*data collector* use *address_cache* file for it own purpose  
only devices listed in address_cache used for collection  
configuration stored into config.visiobas.py module  
--single_thread 1 (using single thread instead of multi thread reads)    
<pre>
cd /opt/visiodesk-gateway
python ./data_collector.py
</pre>

__Visiobas Gateway Server (VGS)__  
default protocol: __JSON-RPC 2.0__

Visiobas Gateway Server should store list of all objects working with.  
Each device object has own unique identifier, due to it VGS get all necessary information about protocols and others data  
  
__API header__  
***
Due to JSON-RPC 2.0 format any messages request and respond has to contain follow properties:
<pre>
{  
    "jsonrpc":"2.0",  
    "id": [number]  
}
</pre>
__API Messages__
***
  
+ __Upload request device list__

Request
<pre>  
{  
    "method":"upload_request_device_list",  
    "params": [{
        TODO describe request device list object
    }]  
}
</pre>
Response  
<pre>
{
    "result": {
        TODO describe format of request device list
    }
}
</pre>

+ __Scan BACnet network__

Request
<pre>
{  
    "method": "scan_bacnet_network",
    "device_id": [number]  
}
</pre>
Response
<pre>
{
    "result": {
        "data": [
            {
                "27": [string] description,
                "72": [number] object identifier,
                "74": [string] object reference,
                "351": [string] device identifier
            }
        ],
        "success": [boolean]
    }
}
</pre>

+ __Reset set point__

Request
<pre>
{  
    "method": "reset_set_point",  
    "params": {  
        "object_id": [number],  
    }  
}
</pre>
_object_id_ - unique object id totally identify certain object  
Response   
<pre>
{
    "result": {
        "success": [boolean]
    }
}
</pre>

+ __Write set point__  
