This system is comprised of these main parts:
1) mysql database that is fed with the following scripts
1.a) getAllRegistrationData.py
1.b) getRecerInfo.py
1.c) getRegistrationDataByRaceID.py
The automation isn't yet done on this
2) A flask app (app.py) that waits for messages, pulls data from the db, and displays it.  This is the part the the customer will see
3) readerLink3.py.  This connects to the R420 RFID reader.  It sends tag detections to the flask app, and it creates a webpage for debugging and power tunning at <raspberry-pi-IP.:4000

There are systemd service files in storage here
There is a listing of python packages required to make the virtual envr work
There is a backup of the autorun file to put chrome into kiosk mode under the root user's home dire .config/autorun/<file name here>

