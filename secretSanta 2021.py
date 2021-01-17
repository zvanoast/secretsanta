import random
import smtplib
import sys
import datetime

class Person:
    def __init__(self, n, e, be):
        self.name = n
        self.email = e
        self.backupEmail = be
        self.matchedSender = False
        self.matchedReciever = False

def main():
    theGroup = []
    year = str(datetime.datetime.now().year)

    fileName = input("Enter the file name containing the participant list, within the same directory: ")
    isUsingGmailSmtp = input("Are you using gmail? (y/n)")

    serverName = ''

    if isUsingGmailSmtp == 'y' or isUsingGmailSmtp == 'Y':
        serverName = 'smtp.gmail.com'
    else:
        serverName = input("Please enter the SMTP server: ")

    email = input("Email: ")
    password = input("Password: ")

    file = open(fileName,'r')
    lines = file.readlines()

    for line in lines:
        parts = line.split(',')
        theGroup.append(Person(parts[0], parts[1], parts[2]))
    file.close()

    nPeople = len(theGroup)

    # shuffle and check if anyone got matched with themselves
    # can plug in more rules here later i.e. no repeats from previous year
    isMatched = False
    while (isMatched == False):
        isMatched = True
        theGroupShuffled = theGroup.copy()
        random.shuffle(theGroupShuffled)

        for i in range(0, nPeople):
            if theGroup[i].name == theGroupShuffled[i].name:
                isMatched = False

    #create a backup file just in case
    file = open('backup ' + year + '.txt','a')
    for i in range (0, nPeople):
        file.write('%s is buying a gift for %s.\n' % (theGroup[i].name, theGroupShuffled[i].name) )
    file.close()

    print("Attempting to connect to server...")

    #send emails to each person
    server = smtplib.SMTP_SSL('smtp.gmail.com')
    server.login(email,password)

    print("Connected to server")

    for i in range (0, nPeople):
        toaddrs = theGroup[i].email
        toaddrs2 = theGroup[i].backupEmail
        matchedPerson = theGroupShuffled[i].name
        subject = 'Secret Santa ' + year
        text = f'You, ({theGroup[i].name}), will be buying a Secret Santa gift for {matchedPerson}. This year\'s price range is $40-$50.'
        msg = f'Subject: {subject} \n\n {text}'
        server.sendmail(email, toaddrs, msg)
        server.sendmail(email, toaddrs2, msg)

        print(f"Email sent to {theGroup[i].name}")
    server.quit()
    
    print("Complete\n")
    exit(0)

if __name__ == "__main__":
    main()
